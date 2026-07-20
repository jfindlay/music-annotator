<!-- juncture-tier: opus -->
<!-- sub-track: R0 (census of Original/) — ROADMAP critical-path head; ends at J1 -->

# PLAN — R0: census of `Original/`

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Classify every remaining top-level dir in `~/Remote/hades/Music/Original/` (**147 counted 2026-07-19**, down from ~218
pre-prune) into a two-axis taxonomy, producing the census artifact that **J1** adjudicates (R3 adapter order and pruning,
rung-ladder shape for R2, not-in-MB default posture) and that **R4a** consumes (inventory of the non-classical corpus the
Act II taxonomy must admit).

**Census-before-policy: classify, don't fix.**  R0 takes no action on any dir — no ingest, no deletion, no MB edits, no
adapter work.  In particular the Discogs question ("is it time to implement R3c?") is *answered by* this census's
not-in-MB population count and adjudicated at J1, not pre-empted here.  The one near-action R0 performs is *evidence
collection* for the already-ingested collision class (dirs the user forgot to delete after a past ingest): the census
marks them delete-candidates with journal evidence; deletion itself is operator work (R5).

**Two-axis taxonomy (design decision, this derivation).**  BACKLOG's flat six-class list conflates two orthogonal
dimensions — "whipper rip" is a *provenance* fact; "not-in-MB" is an *MB-relationship* fact; a whipper rip can be
not-in-MB.  The census records both axes per dir so J1 sees the joint distribution (e.g. how many Presto dirs are also
track-mismatched decides whether R3a depends on R3d).  The flat BACKLOG classes are recoverable as projections.

## Verify gate

- The census tool lives in `scripts/` — **outside every tox gate** (test/mypy/lint/format all target `src/ tests/` only;
  precedent: `scan_nonuniform_depth.py`, "ad-hoc analysis tool, not part of the package").  Sessions are
  **deliverable-checked** (the artifact exists, is complete, and its counts reconcile), not KAT-gated.
- `~/.local/bin/tox -m analyze` must remain green trivially (no `src/`/`tests/` changes are enrolled in this sub-track).
  If a session finds it must touch `src/`, that is unenrolled scope — surface it (additive-reshard signal), do not ride
  through.
- Completeness check at ◆: every one of the 147 dirs carries a value on both axes; `unknown` counts are zero or each
  residual is explicitly user-adjudicated and annotated.

## Taxonomy (frozen at S1 as C-R0-TAX)

**Axis 1 — provenance** (from local evidence only):

| Value | Evidence signature |
|-------|--------------------|
| `bach-edition` | Brilliant Classics Bach Edition remainder (dir naming, existing conventions) |
| `presto` | PrestoMusic download: ISRC-bearing tags, booklet PDFs, Presto artifact files |
| `whipper` | whipper/MakeMKV rip: `whipper.log` / `.cue` / AccurateRip artifacts / rip-log sidecars |
| `amazon` | Amazon Music download: vendor tag signatures, Amazon manifest files |
| `other-download` | Downloaded provenance evident but vendor not identified |
| `unknown` | No provenance signal (must be zero or adjudicated at ◆) |

**Axis 2 — MB status**:

| Value | How determined |
|-------|----------------|
| `already-ingested` | Collision class: journal `source` match (relative path) + destination present.  Delete-candidate. |
| `in-mb-clean` | Embedded `MUSICBRAINZ_ALBUMID`, or Pass 2 search hit whose track/disc counts reconcile |
| `in-mb-mismatch` | MB release found but track counts / edition disagree (feeds R3d population) |
| `not-in-mb` | Pass 2 search authoritatively empty (feeds R3e/R3c adjudication at J1) |
| `non-classical-other` | Audiobooks, Dance, Education, … — MB largely inapplicable; feeds R4a inventory |
| `unknown` | Pass 1 could not determine and Pass 2 not yet run (must be zero at ◆) |

Free-text `evidence` and `notes` fields accompany every row; `ambiguous → user-adjudicated` resolutions record who/why.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 | Census tool + Pass 1 offline evidence sweep | B | Sonnet | journal-detects/tag-adjudicates (NOTES), C-PROV vocabulary | `scripts/census_original.py`, `docs/census-r0.json`, `docs/census-r0.md` |
| 2 ◆ | Pass 2 targeted MB lookups + adjudication + final census artifact | B | Sonnet | C-NET-CORE, C-NET-TERM (via `_discover` search helpers), C-R0-TAX | `docs/census-r0.json`, `docs/census-r0.md` (finalised), `scripts/census_original.py` (Pass 2 mode) |

`Cat`: B = algorithm/analysis.  `◆` = sub-track-final row; **J1 fires at this boundary** (ROADMAP juncture,
`juncture-tier: opus`) consuming the census artifact and the R0 action-frame digest.  No interface-design juncture fires
inside R0: the taxonomy (the sub-track's one design surface) is frozen in this derivation, not designed by an executor.

## Session detail

### S1 — census tool + Pass 1 offline evidence sweep

**Deliverable.**  `scripts/census_original.py` (Pass 1 mode) run against the live mount, emitting
`docs/census-r0.json` (one row per top-level dir: both axis values, evidence fields, per-dir stats) and
`docs/census-r0.md` (human summary: joint-distribution table, per-class dir listings, ambiguity queue for S2).

Pass 1 collects, per top-level dir, **local evidence only — zero network**:

1. **Shape stats** — file formats/extensions, track counts, disc-subdir structure, total size.
2. **Embedded-tag probe** (mutagen, first-file-per-disc sampling with a `--full-scan` flag): `MUSICBRAINZ_ALBUMID`
   (→ `in-mb-clean` + collision candidate), ISRC presence (→ `presto` signal), vendor/comment tag signatures
   (→ `amazon`), genre tags (→ `non-classical-other` signal).
3. **Sidecar artifacts** — `whipper.log`, `.cue`, AccurateRip logs (→ `whipper`); booklet PDFs (→ `presto`); vendor
   manifests (→ `amazon`).
4. **Collision probe** (journal detects, tag adjudicates): parse `Done/music_annotator_journal.json`; join each census
   dir against journal `source` fields **on paths relative to `Original/`** (journal holds canonical
   `/home/findlay/...` paths; the census runs from the `~/Remote/hades/...` vantage — absolute-path joins are the
   documented silent-no-op hazard, NOTES "Note on host paths").  A dir whose files match journal entries *and* whose
   journal destinations exist under `Done/` is `already-ingested` (delete-candidate).  Evidence recorded per dir:
   journal-entry count, destination-present count, source file count.  Optional `--verify-hashes` flag re-verifies
   SHA-256 per file for gold-plated evidence (default **off** — expensive over sshfs; the delete decision is the
   operator's at R5 drain time, made against the recorded evidence level).

**CLI**: `--original`, `--done`, `--journal` path args (defaults to the `~/Remote/hades/Music/*` vantage), `--out`
prefix, `--full-scan`, `--verify-hashes`.  Follows house style (docstrings, types, 128-col) but is **not** enrolled in
the tox gates (see Verify gate).

**Subtleties.**

- **Journal action vocabulary**: repo AGENTS.md describes `action="copied"`; `_pipeline.py:1548` filters
  `action == "tagged"`.  The probe must match the journal's *actual* vocabulary — inspect real entries before coding the
  filter; treat `{"tagged", "copied"}` membership as the candidate set and record which occurred.
- **Read-only invariant**: the script must not write, move, or delete anything under `Original/`, `Done/`, or
  `Reference/`.  Its only outputs are the two artifact files in the repo.
- **sshfs performance**: sample tags (first file per disc dir) by default; full scans opt-in.  147 dirs must complete in
  one session comfortably.

### S2 ◆ — Pass 2 targeted MB lookups + adjudication + final artifact

**Deliverable.**  Every dir carries both axis values; `docs/census-r0.md` finalised with the joint distribution and the
J1 handoff digest appended to this PLAN's action-frame digest.

1. **Pass 2 network mode** (`--pass2`): for dirs Pass 1 left `unknown` on axis 2, search MB via the *existing*
   `_discover` helpers (`search_releases_by_dir` / `_search_mb_releases` — already on `_net` since R1-F, commit
   `e7370b7`).  Read-only; polite-delay and retry posture inherited from C-NET-CORE/C-NET-TERM.  Compare candidate
   releases' track/disc counts against Pass 1 shape stats → `in-mb-clean` / `in-mb-mismatch` / `not-in-mb`.
2. **Adjudication queue**: dirs still ambiguous after Pass 2 are surfaced to the user (Question tool, batched) rather
   than guessed; resolutions recorded with rationale in the `notes` field.
3. **Final artifact**: regenerate `census-r0.json` / `census-r0.md`; the MD leads with the joint-distribution table
   (axis 1 × axis 2 counts) and the per-class listings J1 needs: R3a/R3b/R3c/R3d/R3e populations, `already-ingested`
   delete-candidates with evidence levels, `non-classical-other` inventory for R4a.
4. **J1 handoff**: append the action-frame digest entry (discoveries, distribution surprises, taxonomy strain) — J1 is a
   paged fork and sees only what is written down.

**Subtleties.**

- **Search scope discipline**: Pass 2 queries only dirs unresolved by local evidence — do not re-query dirs with an
  embedded MBID (their status is tag-held).
- **`in-mb-clean` vs `in-mb-mismatch` boundary**: a fuzzy-search hit is not identity.  Track-count reconciliation is the
  minimum bar; when counts disagree across all candidates, classify `in-mb-mismatch` only if some candidate is plausibly
  the same edition, else `not-in-mb` with the near-miss noted.  When in doubt → adjudication queue, not silent choice.
- **Defensive-download invariant** applies unchanged: retries exhausted / cannot-determine → that dir goes to the
  adjudication queue with the failure noted (never silently classified).

**Deferrals.**  Acting on any classification (ingest, deletion, MB edits, adapters) — all post-J1.  The
`already-ingested` drain is R5 operator work.

## Cross-session contracts

### Consumed (frozen upstream — invalidation is a destructive-HALT)

- **C-NET-CORE / C-NET-TERM** (R1, `_net.py`) — Pass 2 reuses the `_discover` search helpers migrated in R1-F
  (`e7370b7`); no new transport code, no new classifier.
- **Journal-detects / tag-adjudicates** and **"Note on host paths"** (prose, `docs/NOTES.md`) — the collision probe is
  a read-only instance of the detect step; relative-path joins are mandatory.
- **Defensive-download posture** (repo `AGENTS.md`) — cannot-determine ≠ no-data, applied to census classification.

### Produced

- **C-R0-TAX** (frozen at S1; consumed by S2, J1, and the R2/R3 PLAN derivations): the two-axis taxonomy — axis
  definitions, class values, and evidence-signature semantics as specified in "Taxonomy" above.  The census artifact
  schema (`census-r0.json` row shape) is an appendix of this contract.  Stability horizon: through J1 and the R2/R3
  derivations that cite census populations; not a runtime contract.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Census tool + Pass 1 offline sweep | done | 63c897b | C-R0-TAX (frozen: two-axis taxonomy + census-r0.json schema) |
| 2 ◆ | Pass 2 + adjudication + final artifact | pending | — | — (artifact finalised) |

## Action-frame digest

*(none yet — S2 appends the J1 handoff entry)*

## Discoveries & risks

- **R-1 (journal action vocabulary).**  `action="copied"` (AGENTS.md prose) vs `action == "tagged"` (`_pipeline.py`
  code) — the probe must be coded against inspected journal reality, not either document.  If the journal contains
  neither for copy events, HALT and surface (documentation/code divergence worth its own fix).
- **R-2 (host-path mapping).**  Journal `source`/`destination` are `/home/findlay/...`; census vantage is
  `~/Remote/hades/...`.  All joins relative to `Original/` / `Done/` roots.  An absolute-path join silently yields zero
  collisions — the exact hazard NOTES documents for maintenance commands.
- **R-3 (sshfs scan cost).**  147 dirs × per-file tag reads over sshfs may be slow; sampling default + opt-in full scan
  bounds it.  If Pass 1 cannot complete in-session even sampled, split the sweep by dir range (internal-continue, not a
  reshard).
- **R-4 (taxonomy strain).**  If a dir class emerges that neither axis admits (e.g. a mixed dir holding two releases),
  extend `notes` and surface at ◆ — J1 evaluates whether C-R0-TAX needs a class added.  Do not silently shoehorn.

## Notes for executors

- **Tier routing.**  S1/S2 are Sonnet (`@build`).  No juncture fires inside R0; **J1 fires at the ◆ boundary** and is
  Opus per the ROADMAP header (`juncture-tier: opus`).
- **Read-only invariant** (S1 subtlety, repeated because it is the sub-track's one destructive-risk surface): the
  census never mutates the music mounts.  Repo-file writes only.
- **No `src/`/`tests/` changes are enrolled.**  `scripts/` + `docs/` only.  A discovered need to touch `src/` is an
  additive-reshard signal.
- **Register: PEDAGOGY off** — the script gets thin mechanical docstrings; the census MD is a data artifact, not an
  essay.
- **Adjudication posture** (S2): batch ambiguous dirs into Question-tool prompts for the user; never guess a
  classification.  The census's value to J1 is that its counts are *trustworthy*.
- **Suggested `/plan-run` invocation**: `/plan-run halt-at-boundaries` — the ◆ boundary is the J1 handoff; halting
  there hands the census review + juncture to the user rather than auto-chaining into J1.
