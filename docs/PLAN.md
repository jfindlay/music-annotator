<!-- Rolling action frame.  The previous sub-track (pre-R6d correctness fixes) closed 2026-08-24 at
     commit 6ee245f; its plan and ledger live in this file's git history.  This sub-track was derived
     2026-08-24 from the first full live-hades preflight analysis — see docs/NOTES.md § "Preflight
     evidence analysis (2026-08-24)" for the evidence base.  Rewritten at the next boundary. -->

# PLAN — preflight-evidence corrective fixes (pre-repair-turn)

## Why this sub-track exists

The first full `preflight` against live hades produced the J3 evidence the roadmap asked for — and the analysis of its
old→new move corpus found **six confirmed defects** in the maintenance recompute engine (defects A–F, root-caused with
file:line evidence in `docs/NOTES.md` § "Preflight evidence analysis (2026-08-24)").  Three are actively destructive:

- **C** — 410 planned moves degrade named performers to `Unknown Performers` (one-directional; zero recoveries).
- **E** — completers erased from composer handles (`Mozart; Süßmayr → Mozart`), contradicting adjudicated SEL-8.
- **F** — 164 of 176 collision suffixes are false positives from plan-blind collision assessment; the executor relies on
  `os.replace` clobber semantics, so ordering accidents can silently overwrite library files.

The remaining three degrade the handle layer: **A** (locale-blind canonical name resolution — arbitrary-locale alias
picks), **B** (broken stability premise of the sanctioned fixed-MBID dereference + `repath` docstring drift), **D**
(release-level-only ensemble rule drops true performing bodies: wind subgroups, choirs).

**The operator repair turn (ROADMAP "Current state" step 2) is blocked until this sub-track closes.**  The legitimate
backlog the repair turn should then execute: ~1,099 leaf renumberings + intermediate renumbering/collapse, 439 year-label
normalisations, 12 genuine consolidations, 83 `mm.nn` eliminations, catalogue-token dir merges.

## Cross-session contracts

Frozen at derivation:

- **C-NOCLOBBER** — a maintenance move NEVER overwrites an existing destination file.  Enforced inside
  `_move_verify_journal` (the single C-MOVE site) with atomic no-replace semantics (existence-check + exclusive-create
  or link-then-unlink; implementation chosen in-session).  A refused move is an error surfaced to the operator, never a
  silent skip and never a clobber.  Test-enforced: a KAT forces a stationary occupant and asserts refusal + no journal
  entry + both files intact.  Rationale (operator, 2026-08-24): maximally protect the authority, provenance,
  completeness, and correctness of written data.
- **C-SEQ** — move plans execute in dependency order: a move whose destination is another plan entry's source runs after
  that entry vacates it (topological order over the move graph; true swap cycles break via an in-directory temp hop that
  stays inside the C-PROV verify-then-journal chain).  Collision assessment subtracts plan-vacated paths: the suffix
  fires only when the occupant is NOT vacated by the same plan AND audio differs (acoustid/length).  Dry-run and real
  execution share the same sequencing logic so preflight evidence is faithful to what execution would do.
- **C-GUARD** — the last-resort ARTIST path fallback treats ARTIST as a release/edition title only on the
  edition-title shape (ARTIST equals ALBUM); a performer name shared by ARTIST and ALBUMARTIST always survives to the
  path.  The original edition-title regression fixture must keep passing.

Frozen at S3 (2026-08-24), consumed by S4–S6:

- **NORM-2 amendment (revised ruling)** — canonical form = the MB artist `name` field verbatim; native script
  universally; aliases evidence-only, never a dereference target; fallbacks inherited from MB's editors (Ashkenazy's
  Latin career name is MB's own judgment — no ru primary alias exists); patronymic-full native forms accepted.
  Dissolution hypothesis verified live 6/6 (Ozawa 小澤征爾, Stravinsky Игорь Фёдорович Стравинский, Richter/Järvi/WPh
  native-Latin names).  Consequence: alias hydration leaves the maintenance path entirely — S4 *deletes* the hydration
  and reduces `canonical_artist_form` to the name field; `fetch_artist_aliases` leaves the path pipeline.
- **Ensemble selection (SEL-23, new)** — ensemble position at release scope = release-level credits ∪ bodies present
  on a modal majority (>50%) of the release's tracks, computed over the release's full track set identically at
  ingest and recompute.  Minority configurations stay credits-only; soloists still never enter (SEL-11).
- **SEL-8 path grammar (REND-27, new)** — composer path component renders the author chain plain, primary leading
  (`Mozart; Süßmayr`); role annotations render in tags only (REND-3); unification direction is upward
  (primary + completer everywhere) per SEL-8, as already adjudicated.
- **C-DET premise repair** — dissolved by NORM-2-as-revised: the canonical form is a scalar MB field, stable under
  alias-list reordering by construction; `repath` becomes genuinely offline again ("embedded tags alone" claim
  restored in S4/S7).  No persistence tag, no backfill shard.

## Sessions

Ordering rationale: S1/S2 are adjudication-free pure-bug fixes with self-contained KATs — they land first so the worst
destruction vectors are closed even if adjudication stalls.  S3 gates the three policy-dependent builds.  Each build
session is one conceptual unit, ~100–300 LOC, 2–4 files, `tox -m analyze` green (100% branch coverage, mypy strict,
pylint 10.00).

| ID | Type        | Deliverable (commit-title shape)                                                      | Deps | Status |
|----|-------------|----------------------------------------------------------------------------------------|------|--------|
| S1 | build       | Fix ARTIST-fallback guard: performer names never degrade to Unknown Performers (C-GUARD) | —    | todo   |
| S2 | build       | Dependency-ordered move execution + vacancy-aware collision assessment (C-NOCLOBBER, C-SEQ) | — | todo   |
| S3 | adjudication| STYLEGUIDE: NORM-2 amendment, ensemble selection case, SEL-8 path grammar, C-DET repair  | —    | todo   |
| S4 | build       | Locale/script-aware canonical name resolution per amended NORM-2 (defects A+B)           | S3   | todo   |
| S5 | build       | Ensemble path component per new selection ruling (defect D)                              | S3   | todo   |
| S6 | build       | Composer-chain unification up to primary+completer per SEL-8 (defect E)                  | S3   | todo   |
| S7 | build       | Register/doc reconciliation: repath docstring, C-DET note, cross-references              | S4   | todo   |
| S8 | operator    | Re-run preflight on hades; acceptance gate; unblock the repair turn                      | all  | todo   |

### S1 — ARTIST-fallback guard (defect C)

Files: `src/music_annotator/_tags.py` (the last-resort fallback in `build_dest_path`), `tests/unit/test_annotator.py`.
Investigate the original edition-title fixture (test_annotator.py ~4846–4933) to confirm its shape is ARTIST == ALBUM;
narrow the guard per C-GUARD.  KATs: self-performed classical (Rachmaninoff shape), pop/jazz (ARTIST == ALBUMARTIST ≠
ALBUM), edition-title regression (must still yield no performer), all asserting the rendered performers component.

### S2 — move sequencing + collision narrowing (defects F + clobber posture)

Files: `src/music_annotator/_pipeline_maint.py` (`_move_verify_journal`, plan execution in `repath`/`regroup`/`unify`),
`src/music_annotator/_pipeline_io.py` (`_assess_collisions` gains the plan-vacated set), tests.  Implement C-NOCLOBBER
and C-SEQ.  KATs: renumbering shift chain (no suffix, correct final layout), true two-file swap (temp hop, provenance
chain intact), genuine occupant-stays collision (suffix applied), forced stationary-occupant clobber attempt (refused,
no journal entry).  Preflight dry-run path must exercise the same sequencing so suffix counts in reports reflect
genuine collisions only.

### S3 — styleguide adjudication (interactive; operator present; architect/dialectic register)

Deliverable: STYLEGUIDE case-register updates (C-CASE append-only discipline) + the frozen rules S4–S6 consume.
Agenda, with the evidence to bring:

1. **NORM-2 amendment** — operator preference: native script universally, fallbacks for unknown/plural/problematic
   forms.  Verify the dissolution hypothesis against live MB records for the observed artists (Ozawa, Stravinsky,
   Richter, Järvi, Ashkenazy, Wiener Philharmoniker): is the MB artist `name` field already the native form?  If yes,
   canonical-form = MB name (aliases as evidence only) both amends NORM-2 and dissolves defect B's hydration.
2. **Ensemble selection case** — adjudicate admission rule for bodies absent from release-level credits: candidate rule
   "release-level ∪ bodies present on all (or modal-majority of) tracks"; must keep the anti-forking property that
   motivated the release-level rule while admitting the Bläser and chorus cases.
3. **SEL-8 path grammar** — completer rendering in the composer component: plain (`Mozart; Süßmayr`) vs role-annotated;
   interaction with the composer-dir dedup and `safe_name`.
4. **C-DET premise repair** — pick the stability mechanism (locale-filtered resolver / MB-name canonical / persist
   resolved form into a tag at annotation time).  If persistence is chosen, scope the backfill (an enrich-shaped
   idempotent pass) as a follow-on shard, not part of this sub-track.

### S4 — canonical name resolution (defects A + B)

Files: `src/music_annotator/_artists.py` (`canonical_artist_form`), possibly `src/music_annotator/_tags.py`
(hydration removal if S3 dissolves it), `src/music_annotator/_mb_api.py` (only if the fetch surface changes), tests.
Implement the S3 rule; deterministic under alias-list reordering (KAT: same artist, shuffled alias order, same result).
Locale-rich alias fixtures for each observed failure shape.  If hydration leaves the maintenance path, restore the
`repath` "embedded tags alone" claim and delete the now-dead `--user-agent` requirement from the offline passes'
docstrings (the CLI flags stay — ingest still needs them).

### S5 — ensemble path component (defect D)

Files: `src/music_annotator/_tags.py` (performers component), `src/music_annotator/_pipeline_maint.py` (unify's
canonical-path recompute consumes the same rule), tests.  KATs: wind-subgroup release (subgroup survives), choral work
with per-track chorus (chorus survives), the anti-forking regression (per-track soloist/subgroup variation must not
fork top dirs — the original fragmentation shape stays fixed).

### S6 — composer chain per SEL-8 (defect E)

Files: `src/music_annotator/_pipeline.py` (work-group composer unification direction), `src/music_annotator/
_pipeline_maint.py` (the classical arranger/finisher retroactive pass follows the same direction), tests.  KATs: the
completion shape (some movements credit the completer as "composer (additional)") unifies UP — every movement renders
primary + completer per the S3 grammar; a plain single-composer work is unchanged; the composer-split (non-classical)
pre-processing is untouched.

### S7 — register/doc reconciliation

Files: docstrings in `_pipeline_maint.py` (repath), `docs/NOTES.md` (C-DET note updated to the repaired premise),
`__main__.py` epilogs if the offline passes' network posture changed in S4.  No behaviour change; `tox -m analyze`
still green.  Verify `__init__.py` `__all__` needs no update.

### S8 — operator acceptance gate

Re-run `preflight` on hades.  Acceptance criteria against the fresh report and move corpus:

- `Unknown Performers` introductions: **0**.
- Locale/alias swaps against the S3-adjudicated forms: **0**.
- Collision suffixes: only genuine occupant-stays collisions (expected ≈ 12 from the 2026-08-24 corpus).
- `mm.nn` double numberings in planned paths: **0**.
- Completer and ensemble handles match the S3 rulings on the known cases (K.626/K.412 shapes, Bläser, choral works).

On acceptance: proceed to the repair turn (ROADMAP "Current state" step 2 — `repath` first, then per the report), and
rewrite this PLAN at the boundary.

## Notes for executors

- **Register rule** (repo AGENTS.md): durable files state the property/invariant, never the plan coordinate.  Anneal
  denylist for this sub-track: `\bS[1-8]\b` (session ids), `preflight-evidence corrective`, `repair turn`,
  `sub-track`, `plan-run`.  Contract names (C-NOCLOBBER, C-SEQ, C-GUARD, C-DET, C-MOVE, C-PROV, SEL-*, NORM-*) are
  legitimate durable vocabulary.
- Full gate before declaring any session done: `~/.local/bin/tox -m analyze` (100% branch coverage, mypy strict,
  pylint 10.00/10, ruff, pyupgrade).
- Patch targets bind where the name is imported, not where it originates (repo testing convention).
- The C-PROV/C-MOVE provenance chain is inviolable: no journal entry before SHA + `_verify_copy` pass; S2's temp-hop
  design must keep every hop inside that chain.
- Evidence base for all defect claims: `docs/NOTES.md` § "Preflight evidence analysis (2026-08-24)"; raw artifacts on
  the dev mount at `~/Remote/hades/Music/log.{json,out}` (hades paths: `/home/findlay/Music/`).

## Progress ledger

VERIFY: `~/.local/bin/tox -m analyze` (combined gate: tests + 100% branch coverage + mypy strict + pylint 10.00 + ruff + pyupgrade). One green run satisfies tests, types, lint, format, and coverage.

| ID | Title                                                                 | Status  | Commit | Notes |
|----|-----------------------------------------------------------------------|---------|--------|-------|
| S1 | Fix ARTIST-fallback guard (C-GUARD)                                   | done    | f09b31d | C-GUARD frozen |
| S2 | Dependency-ordered move execution + vacancy-aware collision (C-NOCLOBBER, C-SEQ) | done    | e71256d | C-NOCLOBBER, C-SEQ frozen |
| S3 | STYLEGUIDE adjudication (NORM-2, ensemble, SEL-8, C-DET)              | done    | f08468a | all four frozen; NORM-2-as-revised, SEL-23, REND-27, C-DET-dissolution |
| S4 | Locale/script-aware canonical name resolution (defects A+B)           | done    | f95e953 | NORM-2-as-revised implemented; alias hydration deleted |
| S5 | Ensemble path component per new selection ruling (defect D)           | pending | —      | depends S3 |
| S6 | Composer-chain unification up to primary+completer per SEL-8 (defect E) | pending | —    | depends S3 |
| S7 | Register/doc reconciliation: repath docstring, C-DET note             | pending | —      | depends S4 |
| S8 | Re-run preflight on hades; acceptance gate                            | pending | —      | depends all; operator |

Frozen contracts: C-NOCLOBBER (implementation confirmed e71256d), C-SEQ (implementation confirmed e71256d), C-GUARD (implementation confirmed f09b31d), NORM-2-as-revised, SEL-23, REND-27, C-DET-repair-by-dissolution (all four frozen at S3, 2026-08-24; STYLEGUIDE register updated).

## Action-frame digest

(append non-trivial discoveries, contract flexes, and notable texture here as sessions run)

- S3 (2026-08-24): dissolution hypothesis verified live against MB, 6/6 — the artist `name` field is already the
  native/preferred form for every observed artist, including the fallback shape (Ashkenazy has *no* ru primary alias;
  MB's editors already chose the Latin career name).  All four rulings landed on the recommended options.  S4 shrinks
  materially: delete hydration rather than build a locale resolver; no backfill shard.  STYLEGUIDE: NORM-2 revised in
  place, SEL-23 + REND-27 appended, CE-divergence entry added, §3.1 and §2.3 body text aligned.
