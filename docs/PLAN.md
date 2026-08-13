<!-- juncture-tier: opus -->
<!-- sub-track: R6c (AcoustID tag naming + semantics — Picard alignment) — library-completion arc
     (docs/ROADMAP.md), Act III-a.  Two persisted-tag migrations over the AcoustID/Chromaprint tags:
     (1) unify the ACOUSTID_ID value source on the fingerprint /v2/lookup cluster UUID everywhere
     (Picard-exact), and (2) rename CWP-adjacent CHROMAPRINT_FP -> ACOUSTID_FINGERPRINT with a
     dual-read transition.  CODE-ONLY: the destructive library-wide repatch rides R6d's one J3-gated
     pass (D-A5 precedent); this shard builds + freezes the machinery and proves it on fixtures via
     the src/tests gate.  This IS a /plan-run target: the tag-policy contract + the forward-write
     changes + the offline repatch pass + tests, verifiable by the src/tests gate; the fresh
     library-state scan is the S4 gating step (operator mounts the library). -->

# PLAN — R6c: AcoustID tag naming + semantics (Picard alignment)

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

music-annotator writes two AcoustID-related tags whose form diverges from MusicBrainz Picard, the
project's tag-convention anchor.  Two independent divergences:

1. **`ACOUSTID_ID` value source is inconsistent across writer paths.**  The main ingest pipeline fills
   `ACOUSTID_ID` from `fetch_acoustid_id(recording_mbid)` — the AcoustID `/v2/track/list_by_mbid`
   endpoint (`_mb_api.py:1080`, written at `_pipeline.py:1713`).  The `enrich(re_resolve=True)`
   maintenance pass fills it from `_fetch_acoustid_lookup_raw` — the `/v2/lookup` fingerprint endpoint
   (`_pipeline_maint.py`).  Picard defines `acoustid_id` as *"the ID returned as a result for the
   fingerprint lookup on acoustid.org"* — i.e. the `/v2/lookup` cluster UUID.  A file touched by both
   paths can carry two *different* UUIDs at different times, and the `audit` journal-vs-tag compare
   (`_audit.py:203`) then flags a **spurious mismatch**.

2. **The Chromaprint fingerprint is stored under the non-Picard key `CHROMAPRINT_FP`.**  Picard's key
   is `acoustid_fingerprint`.  The value is already stored (the raw Chromaprint string); only the key
   diverges.

The operator intent (BACKLOG "AcoustID tag naming + semantics", deferred 2026-07-14 until library
annotation is mostly complete): keep **both** AcoustID values (the cluster UUID and the fingerprint),
make them **consistent** and **Picard-conformant**.

**The structural facts that shape this shard (survey 2026-08-13 — they correct BACKLOG's premise).**

- **BACKLOG's "two semantically different UUIDs (track vs cluster)" framing is imprecise.**
  `fetch_acoustid_id`'s `list_by_mbid` value is itself an AcoustID **cluster** UUID — its own docstring
  states it "is a cluster identifier that groups all crowd-sourced Chromaprint fingerprint submissions
  for the same track" (`_mb_api.py:1084`).  So both paths already emit a *cluster* UUID; they differ
  only in the **lookup key** — recording-MBID-keyed (`list_by_mbid`) vs fingerprint-keyed
  (`/v2/lookup`).  The two can still *disagree* (a recording MBID may map to a different cluster than
  the fingerprint lookup surfaces), so the audit-mismatch problem is real — but this is a
  *value-source unification*, not a *value-type* fix.
- **The Picard-conformant source is already computed and already called at ingest.**  The pipeline
  already runs fpcalc at ingest (`final_tags.chromaprint_fp = _run_fpcalc(src_file)`,
  `_pipeline.py:1217`) and already calls the `/v2/lookup` fingerprint endpoint at ingest
  (`_pipeline.py:1226`) — but **discards** the returned cluster UUID (`_confirm_mbids, _ = ...`),
  using it only for identity-confirm logging.  So unifying `ACOUSTID_ID` on the fingerprint-lookup
  cluster UUID adds **no new fpcalc dependency** and no new network call at ingest: it captures the
  already-fetched-and-discarded `[1]` value and drops the separate `fetch_acoustid_id` write.

**The one genuine regression to rule on (the S1 inflection).**  The fingerprint-lookup source is only
available when an AcoustID **api_key is supplied** and fpcalc yielded a **non-empty fingerprint**.
The retired `list_by_mbid` source needed **neither** (recording-MBID-keyed, no key).  So switching the
source means: when no api_key / no fingerprint, `ACOUSTID_ID` would be **empty at ingest** where it was
previously filled cheaply.  The S1 judgment is the fallback policy (see posture 1 below).

**Sequencing (D-A5/D-A7 precedent).**  Code-only: the forward-write changes and the offline repatch
pass are built and unit-proven; the destructive library-wide repatch is R6d's one J3-gated pass.  No
destructive library operation in R6c.  Matches R6a / R6b.

**Interface posture (resolved at this PLAN derivation — the S1 inflection judgments):**

1. **Unify `ACOUSTID_ID` on the fingerprint `/v2/lookup` cluster UUID everywhere; on no-key/no-
   fingerprint, leave `ACOUSTID_ID` empty at ingest and let `enrich` fill it later — do NOT fall back
   to `list_by_mbid`.**  Chosen over a `list_by_mbid` fallback because a fallback re-introduces the
   dual-source divergence this shard exists to remove (the audit could still see two different cluster
   UUIDs).  Picard writes `acoustid_id` only from the fingerprint lookup; an empty tag pending enrich
   is honest and Picard-consistent.  **Tradeoff:** worse on immediate completeness — a no-api-key
   ingest now leaves `ACOUSTID_ID` empty where `list_by_mbid` filled it for free.  Accepted because
   (a) the value is recoverable offline later via `enrich(re_resolve, acoustid_key=…)`, (b) the empty
   is the correct provisional state (no fingerprint-confirmed AcoustID identity yet), and (c) it
   removes the dual-source divergence permanently rather than papering over it.  **Reopen trigger:** if
   the operator's ingest flow routinely runs without an api_key, the empty-at-ingest cost is larger
   than estimated — surface as a discovery (a `list_by_mbid`-fallback additive-reshard), do not
   silently restore the fallback.

2. **`CHROMAPRINT_FP` → `ACOUSTID_FINGERPRINT` rename with a dual-read transition; write the new key,
   read both.**  The forward write emits `ACOUSTID_FINGERPRINT`; every read-back helper accepts both
   the new key and the legacy `CHROMAPRINT_FP`, so a mixed library (old files not yet repatched) reads
   correctly throughout the transition.  Chosen over a hard rename (write-new/read-new-only) because
   existing files carry the legacy key and would silently lose the fingerprint on read until R6d's
   destructive repatch lands.  **Tradeoff:** the dual-read is permanent code weight (a two-key read
   until every file is migrated *and* the legacy-read is deliberately retired) — worse on eventual
   simplicity than a hard cutover, accepted because a persisted-key migration over a library that is
   not repatched in this shard has no safe hard-cutover point.

3. **Code-only; destructive library-wide repatch rides R6d (D-A5 precedent).**  This shard builds and
   freezes the machinery (forward-write + offline repatch pass) and proves it on fixtures via the
   src/tests gate; it does **not** run the repatch destructively on the live library.  R6d runs it
   under J3 as one part of its one-pass — and this machinery adds an **AcoustID tag-content-repatch
   capability** to R6d's paths-only engine (the same gap R6b closed for the catalogue-colon case).
   **Tradeoff:** the existing library stays on the legacy `CHROMAPRINT_FP` key and the dual-source
   `ACOUSTID_ID` until R6d — the accepted D-A4/D-A6-style temporary inconsistency (the dual-read makes
   it harmless), worse on immediate library uniformity than an in-shard repatch, accepted to keep this
   shard off J3 and inside the fast src/tests inner loop.

The four sessions, in landing order:

1. **S1 @architect — AcoustID tag policy substrate.**  Freeze **C-ACID**: the `ACOUSTID_ID`
   value-source rule (fingerprint-lookup cluster UUID; the no-key/no-fingerprint empty-not-fallback
   ruling), the `CHROMAPRINT_FP` → `ACOUSTID_FINGERPRINT` rename + the dual-read transition contract,
   and the `audit`-compare mismatch semantics under the unified source.  No pipeline mutation yet —
   S1 lands the policy + its KAT witnesses.
2. **S2 — Forward-write alignment (pipeline + tagger + models + read helpers + audit).**  Rewrite the
   ingest write to capture the already-fetched `/v2/lookup` cluster UUID and drop the separate
   `fetch_acoustid_id` write; rename the fingerprint key on the write path (`_MP3_TXXX_MAP`, the FLAC
   key, the model fields); make the read helpers dual-read; update the audit compare.  Consumes
   C-ACID.
3. **S3 — Offline AcoustID repatch pass.**  Add the maintenance pass (in `_pipeline_maint.py`,
   modelled on `enrich` / `repatch_catalogue_colon`) that migrates existing files: re-source
   `ACOUSTID_ID` (via `enrich`'s existing `/v2/lookup` path when a key is available) and rewrite the
   legacy `CHROMAPRINT_FP` under the new `ACOUSTID_FINGERPRINT` key, on the re-tag→`_verify_copy`→
   journal provenance chain, `dry_run`-aware and idempotent.  Consumes C-ACID.  Not run destructively.
4. **S4 ◆ — Library-state scan + census + register anneal.**  New scanner for legacy `CHROMAPRINT_FP`
   keys and dual-source `ACOUSTID_ID` state across the library; census the population R6d's repatch
   will migrate (distinguish scan-not-run from no-findings); validate the repatch against a
   representative fixture; close the sub-track; anneal the planning register.

## Verify gate

Discovered from `pyproject.toml` (tox envs); do not assume `make`.  Both **binding** — this is a code
sub-track.  (Confirmed green at shard time: `~/.local/bin/tox -e test` → 1758 passed, 100.00% branch
coverage.)

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage enforced**,
  `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict).
- Full gate before any row is declared done: `~/.local/bin/tox -m analyze` (build + test + check_type +
  check_format + check_lint 10.00/10 + check_upgrade).  The AGENTS.md "never skip `tox -m analyze`" rule
  applies to every row.  Import order via `~/.local/bin/tox -m edit`, never hand-edited.
- **S4 scan step is not gate-covered:** the new scanner lives outside `src/`+`tests/` (like
  `scan_nonuniform_depth.py` / `scan_fragmentation.py` / `scan_catalogue_colon.py`); it runs clean under
  `venv/bin/python -m py_compile` and best-effort `venv/bin/mypy scripts/` but is not `tox`-enforced.
  Its gating role is producing a fresh scan the S4 ◆ review consumes, not passing the gate.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 @architect | Freeze the Picard-aligned AcoustID tag policy (value source + key rename + dual-read) | A | Opus | Picard tag convention (AGENTS.md anchor), C-AR (archival triple) | `src/music_annotator/models.py`, `src/music_annotator/_tagger.py`, `tests/unit/test_models.py` |
| 2 | Align the AcoustID forward-write path to the frozen policy | B | Sonnet | **C-ACID** | `src/music_annotator/_pipeline.py`, `src/music_annotator/_tagger.py`, `src/music_annotator/_pipeline_io.py`, `src/music_annotator/_audit.py`, `tests/unit/test_pipeline.py` |
| 3 | Migrate existing files in an offline AcoustID repatch pass | B | Sonnet | **C-ACID** | `src/music_annotator/_pipeline_maint.py`, `tests/unit/test_pipeline_maint.py` |
| 4 ◆ | Scan the library for legacy AcoustID tag state + census + anneal | I | Sonnet | **C-ACID**, `scan_acoustid_tags.py` | `scripts/scan_acoustid_tags.py`, `docs/BACKLOG.md`, `tests/unit/test_pipeline_maint.py` |

`Cat`: **S1 is A (substrate)** — freezes **C-ACID**, the tag policy every later session and R6d read;
over-specify (carry the dual-read transition contract and the value-source rule even though S2 is the
first consumer).  **S2 is B** — the forward-write mechanics over the frozen policy.  **S3 is B** — the
offline migration mechanics, modelled on the existing `enrich` / `repatch_catalogue_colon` passes.
**S4 is I (integrative)** — the library-state scan + census refresh give the contract its
operator-visible/durable form (the scan is what R6d's repatch runs against), close the ◆, carry the
anneal.

`Tier`: **S1 is Opus + `@architect` inflection.**  The value-source unification is permanent
library-wide tag policy, and the no-key/no-fingerprint fallback ruling is a real ingest-behaviour
regression (empty `ACOUSTID_ID` where `list_by_mbid` filled it) that tests alone cannot adjudicate —
lever 3 (design-error cost: a wrong fallback re-introduces the divergence) and lever 4
(correctness-criticality: `ACOUSTID_ID` is an archival identity dimension).  **S2, S3, S4 are Sonnet**
— mechanical over the frozen policy with a strong inner loop (lever 5: 100% branch coverage + strict
mypy) and direct write-pass precedents (`enrich`, `repatch_catalogue_colon`) to model.
`juncture-tier: opus` — kept (arc default).

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.

- **S1 ≈ 80–160 LOC, 2–3 files** (the model-field rename + key-map rename + the policy KATs; the
  policy itself is small — the value-source rule and dual-read are declarative).  Under/within band.
  **Irreducible unit (lever 2, floor):** the value-source rule, the key rename, and the dual-read
  transition are one contract — the rename with no dual-read strands existing files; the value rule
  with no audit-semantics update leaves the spurious-mismatch bug.  Kept whole.  **Lever 3/4:** high
  cost-of-wrong / correctness-crit is *why* S1 is Opus+inflection, not why it fractures.  One-line
  title passes.
- **S2 ≈ 150–300 LOC, 4–5 files** (capture the `/v2/lookup` UUID + drop the `fetch_acoustid_id` write
  in `_pipeline.py`; rename the key in `_tagger.py` `_MP3_TXXX_MAP` + FLAC; dual-read helpers in
  `_pipeline_io.py`; audit compare in `_audit.py`; tests).  Within band.  **Bundles both migrations'
  forward-write** — legitimate: they touch the same tagger/model/read-helper files and there is no
  consume-dependency between the value change and the rename, so splitting them would create a
  half-boundary (see 5-session alternative, rejected).  One-line title: "Align the AcoustID
  forward-write path to the frozen policy" — passes.  **Lever 1 (ambient complexity):** touches the
  hot copy/tag/verify path — but only the AcoustID write region; the provenance chain is unchanged.
- **S3 ≈ 120–220 LOC, 2 files** (the offline repatch pass + tests, modelled on
  `repatch_catalogue_colon`).  Within band.  **Separate session by the one-line-commit-title
  corollary** — "migrate existing files" is distinct from "change what new files get written"; split
  at the contract-sharp C-ACID boundary (S1 freezes the policy, S2 does forward-write, S3 migrates the
  back-catalogue).  **Lever 1:** two direct precedents (`enrich` offline `/v2/lookup` re-source;
  `repatch_catalogue_colon` key-rewrite on the provenance chain) — not greenfield; not fractured below
  the floor.
- **S4 ≈ 60–120 LOC + scan run, 2–3 files** (new scanner + census + a no-regression repatch parity
  test + anneal).  Under band; **separate by the corollary** — the scan/census/anneal is one
  integrative unit; merging into S3 yields an "and"-joined title.  Not fractured below the floor (the
  scan validates the population the census reports).

## Session detail

### S1 @architect — Freeze the Picard-aligned AcoustID tag policy — freezes C-ACID

**Deliverable.**  The tag policy and its type/key surface, no pipeline mutation:
- **Model + key rename.**  Rename the fingerprint field `chromaprint_fp` → `acoustid_fingerprint` on
  `TrackTags` (`models.py:1490`) and `TransactionEntry` (`models.py:1811`); rename the tag-key map
  entry `CHROMAPRINT_FP` → `ACOUSTID_FINGERPRINT` in `_tagger.py` (`_MP3_TXXX_MAP:117`, TXXX desc
  `"Acoustid Fingerprint"`; FLAC Vorbis key `acoustid_fingerprint`).  Keep the archival-triple comment
  (`# extensible: 4th dim slots in here`) and C-AR field ordering intact.
- **Policy declarations (prose + typed contract).**  State the `ACOUSTID_ID` value-source rule (the
  fingerprint `/v2/lookup` cluster UUID is the single source; empty-not-`list_by_mbid` when no
  key/fingerprint) and the dual-read transition rule (write `ACOUSTID_FINGERPRINT`, read both keys) as
  the frozen C-ACID contract — the read/write mechanics land at S2, but the *rule* freezes here.
- Docstrings state the property (Picard-aligned AcoustID tag naming; single fingerprint-lookup source;
  dual-read transition), citing the Picard convention (AGENTS.md anchor), never the plan coordinate.

**KAT (the freeze witness for C-ACID).**  In `test_models.py` (+ a tagger round-trip test):
(a) **key rename round-trips** — a `TrackTags` with `acoustid_fingerprint` set writes the
`ACOUSTID_FINGERPRINT` FLAC key / `"Acoustid Fingerprint"` TXXX desc and reads back equal (both
formats);
(b) **legacy key still reads** (dual-read witness at the model/tagger seam) — a file dict carrying the
legacy `CHROMAPRINT_FP` key populates `acoustid_fingerprint` (the transition contract; the read side
lands fully at S2 but S1 pins the model-level expectation);
(c) **archival-triple integrity** — `audio_hash` / `acoustid_id` / `acoustid_fingerprint` all present,
C-AR ordering preserved, no field dropped by the rename;
(d) **value-source rule documented and asserted at the contract level** — a test that pins the frozen
rule (e.g. a policy constant or the documented empty-on-no-key expectation) so S2 cannot regress it.

**Subtleties.**
- **The value-source fallback inflection (the `@architect` judgment).**  The load-bearing ruling:
  when no api_key / no fingerprint, `ACOUSTID_ID` is **empty at ingest**, NOT re-filled from
  `list_by_mbid`.  A `list_by_mbid` fallback re-introduces the dual-source divergence this shard
  removes.  Freeze the empty-not-fallback rule; the reopen trigger is an operator ingest flow that
  routinely runs keyless (then the empty-at-ingest cost is larger than estimated — a fallback
  additive-reshard, surfaced as a discovery).
- **The rename is a persisted-key migration — dual-read is mandatory.**  Existing library files carry
  `CHROMAPRINT_FP`; a write-new/read-new-only rename silently drops the fingerprint on read until R6d.
  Freeze the dual-read (read both keys) as part of C-ACID.
- **`fetch_acoustid_id` is not deleted at S1.**  Its removal from the *write path* is S2's forward-
  write change; the function may remain exported (BACKLOG deferred; a keyless MBID→cluster lookup is
  still a legitimate helper).  S1 freezes the policy that it is no longer the `ACOUSTID_ID` *source*.
- **100%-branch-coverage gate.**  The dual-read branch (new key present / legacy key present / neither)
  needs explicit tests; any `match/case` gets `case _: # pragma: no cover` if exhaustive.

**Deferrals.**  No pipeline/audit forward-write change (S2); no offline repatch pass (S3); no library
scan/census (S4); no destructive repatch (R6d).

### S2 — Align the AcoustID forward-write path to the frozen policy

*(Lower-fidelity sketch — correct for a post-substrate row; crisply specified after C-ACID freezes at S1.)*

**Deliverable.**  Apply the frozen policy to the forward (new-ingest) path:
- **`ACOUSTID_ID` value source (`_pipeline.py`).**  In the copy/tag loop, capture the cluster UUID from
  the `/v2/lookup` call already made at `_pipeline.py:1226` (currently `_confirm_mbids, _ = …` — take
  the `[1]`) and write it to `final_tags.acoustid_id`; **remove** the separate per-track
  `fetch_acoustid_id(rec_id)` write at `_pipeline.py:1713`.  When no api_key / no fingerprint, leave
  `acoustid_id` empty (the frozen empty-not-fallback rule).
- **Key rename on the write + read paths.**  Ensure `_tagger.py` writes `ACOUSTID_FINGERPRINT`; make
  the read helpers (`_read_chromaprint_fp_tag` → renamed, and `_read_tags_flac`/`_read_tags_mp3`
  reconstruction) **dual-read** both `ACOUSTID_FINGERPRINT` and legacy `CHROMAPRINT_FP`.
- **Audit compare (`_audit.py`).**  Update `_DIFF_FIELDS` / `_audit_tag_adjudication` for the renamed
  field and the unified `ACOUSTID_ID` source so a re-resolve overwrite is no longer a spurious mismatch.
- No maintenance pass, no library run.

**KAT (behavioural witness).**  A full-pipeline ingest with an api_key + fingerprint writes
`ACOUSTID_ID` = the `/v2/lookup` cluster UUID (not the `list_by_mbid` value) and `ACOUSTID_FINGERPRINT`
(not `CHROMAPRINT_FP`); an ingest with no api_key writes empty `ACOUSTID_ID` (no `list_by_mbid`
fallback); a file carrying legacy `CHROMAPRINT_FP` reads back its fingerprint via the dual-read; the
audit no longer flags a file re-resolved by `enrich` as a mismatch.

**Subtleties.**
- **The `/v2/lookup` call is already made — reuse it, don't add a second.**  `_pipeline.py:1226`
  already fetches; S2 captures the discarded UUID.  Do **not** add a new network call.
- **Provenance chain unchanged.**  The AcoustID write is inside the existing copy/tag/verify loop; do
  not touch the `_verify_copy` → journal ordering (confirmation-provenance invariant).
- **Dual-read is a read-widen, not a write-fork.**  Write only the new key; read both.
- **match/case / branch coverage.**  Cover key-present-new / key-present-legacy / key-absent and
  api_key-present / api_key-absent.

**Deferrals.**  No offline migration of existing files (S3); no library scan (S4); no destructive run (R6d).

### S3 — Migrate existing files in an offline AcoustID repatch pass

*(Lower-fidelity sketch — post-substrate row.)*

**Deliverable.**  A new offline maintenance pass (in `_pipeline_maint.py`, modelled on `enrich` /
`repatch_catalogue_colon`) that:
- Resolves current on-disk paths via `_resolve_current_lib(journal)`; for each FLAC/MP3, migrates the
  legacy `CHROMAPRINT_FP` value to the `ACOUSTID_FINGERPRINT` key, and (when an api_key is available)
  re-sources `ACOUSTID_ID` from `/v2/lookup` via the existing `_fetch_acoustid_lookup_raw` path — the
  same source `enrich(re_resolve)` already uses.
- Writes via `apply_tags_flac` / `apply_tags_mp3` on the **`enrich` provenance chain**: re-tag →
  `_verify_copy` → append a journal entry only after verification (a new `action`, e.g.
  `"acoustid-repatched"`).  Idempotent (a second run on a migrated library is a no-op) and
  `dry_run`-aware.
- **Not run destructively on the live library** — proven on fixtures; R6d drives it under J3.

**KAT (behavioural witness).**  A fixture FLAC/MP3 with legacy `CHROMAPRINT_FP` + a stale-source
`ACOUSTID_ID` → after the pass, the fingerprint reads back under `ACOUSTID_FINGERPRINT`, the legacy key
is gone, `ACOUSTID_ID` is re-sourced (or left when no key); a `dry_run` writes nothing; a second run is
a no-op (idempotency); an already-migrated file is untouched (no-regression).

**Subtleties.**
- **Model on `repatch_catalogue_colon` / `enrich`, don't invent.**  Both are existing offline
  tag-content write passes with the idempotent / dry-run / re-tag→`_verify_copy`→journal shape.  Reuse
  it — this is R6b's precedent one node later.
- **Key migration = write-new + drop-legacy.**  The pass writes `ACOUSTID_FINGERPRINT` and removes the
  legacy `CHROMAPRINT_FP` key (the forward path's dual-read covers the transition; the repatch retires
  the legacy key per file).
- **`ACOUSTID_ID` re-source is api_key-gated.**  When no key is available the pass migrates the key
  only and leaves `ACOUSTID_ID` for a later keyed run — consistent with S1's empty-not-fallback rule.
- **Provenance chain load-bearing.**  Do not append the journal entry before `_verify_copy` confirms.

**Deferrals.**  No library scan/census (S4); no destructive library run (R6d).

### S4 ◆ — Scan the library for legacy AcoustID tag state + census + anneal

*(Lower-fidelity sketch — post-substrate integrative row.)*

**Deliverable.**  Validate the population and census the migration scope:
- New `scripts/scan_acoustid_tags.py` (standalone, `scan_nonuniform_depth.py` precedent): scan the
  **complete library** for files still carrying the legacy `CHROMAPRINT_FP` key and for `ACOUSTID_ID`
  values that came from the retired `list_by_mbid` source where a re-source is warranted.
  **Distinguish scan-not-run** (unmounted/empty root → never report clean) **from no-findings** (the
  R4b D-1 / R6a D-3 / R6b D-3 hazard); if unmounted at execution, record the census as *not run*.
- Census the population R6d's AcoustID repatch will migrate (how many files carry the legacy key; how
  many carry a divergent `ACOUSTID_ID`).  A signature the S1 policy mis-handles (e.g. a file where the
  fingerprint lookup and the MBID lookup return clusters that *should* agree but don't) is the reopen
  trigger — surface as a discovery; do not silently absorb.

**KAT.**  A no-regression parity test asserting the S2/S3 forward-write + repatch behaviour still holds
against a representative fixture (the integrative session's behavioural pin).

**Subtleties.**  No `src/` change in S4 unless a scanner helper is promoted (it should not be — keep the
scanner standalone per the `scan_fragmentation.py` precedent).  Purely a scan-validation + census +
anneal row; **no destructive library operation** (R6d runs the repatch under J3).

**◆ boundary (register anneal).**  Re-read Purpose.  Confirm all four sessions enacted, `tox -m analyze`
green, ledger complete.  **Planning-register anneal:**
- Durable files (`models.py`, `_tagger.py`, `_pipeline.py`, `_pipeline_io.py`, `_audit.py`,
  `_pipeline_maint.py`, `scan_acoustid_tags.py` docstrings/comments) carry **no plan coordinates** — no
  "S1/S2/S3/S4", no "R6c", no "AcoustID sub-track", no `/plan-run` vocabulary.  State the
  property/reason/invariant (e.g. "the AcoustID cluster UUID from the fingerprint /v2/lookup — Picard's
  `acoustid_id` source"), never the plan coordinate.
- Grep the durable files against the **anneal denylist** (Notes for executors); translate any leaked
  coordinate into standalone prose.
- Report to the library-completion roadmap: the AcoustID Picard-alignment machinery is enacted; C-ACID
  frozen.  **R6d coordination noted** — the machinery adds an AcoustID tag-content-repatch capability;
  R6d runs the destructive library-wide repatch under J3 (this sub-track lands the machinery, not the
  destructive run).

## Cross-session contracts

### C-ACID — the Picard-aligned AcoustID tag policy *(to be frozen at S1 — inflection design)*

**Value source (frozen at S1).**  `ACOUSTID_ID` is the AcoustID **cluster UUID from the fingerprint
`/v2/lookup`** endpoint (`results[0]["id"]`) — Picard's `acoustid_id` source — on **every** writer
path (ingest and enrich).  The retired `fetch_acoustid_id` `/v2/track/list_by_mbid` value is no longer
a source.  **When no api_key is supplied or fpcalc yields no fingerprint, `ACOUSTID_ID` is left empty
at ingest** — it is **not** re-filled from `list_by_mbid` (the empty-not-fallback rule; a keyed
`enrich` fills it later).  **Invariant:** a file's `ACOUSTID_ID` is either empty or a fingerprint-lookup
cluster UUID — never a `list_by_mbid` value — so the `audit` journal-vs-tag compare cannot flag a
spurious dual-source mismatch.

**Key rename + dual-read transition (frozen at S1).**  The raw Chromaprint fingerprint is stored under
the Picard key **`ACOUSTID_FINGERPRINT`** (FLAC Vorbis `acoustid_fingerprint`; MP3 TXXX desc
`"Acoustid Fingerprint"`), renamed from the legacy `CHROMAPRINT_FP`.  The model field is
`acoustid_fingerprint` on `TrackTags` and `TransactionEntry`.  **Dual-read transition:** the forward
path writes only `ACOUSTID_FINGERPRINT`; every read-back helper reads **both** the new key and legacy
`CHROMAPRINT_FP`, so a mixed (partially-migrated) library reads correctly throughout.  The legacy key
is retired per-file by the S3 repatch pass; the dual-read is retained until the library is fully
migrated (R6d) and its removal is a later, explicit decision — not part of this sub-track.

**Resolved interface (to be frozen at S1 — the juncture fork writes the concrete field/key names +
the read-helper signatures into this subsection at execution time).**  *Not yet frozen — to be frozen
at S1.*  Expected surface: `TrackTags.acoustid_fingerprint` / `TransactionEntry.acoustid_fingerprint`;
`_MP3_TXXX_MAP["ACOUSTID_FINGERPRINT"] = "Acoustid Fingerprint"`; a dual-read fingerprint helper
(renamed from `_read_chromaprint_fp_tag`) that returns the new-key value else the legacy-key value; the
`_pipeline.py` capture of the `/v2/lookup` `[1]` UUID; the `_audit.py` `_DIFF_FIELDS` rename.

**Flavour:** compiler-enforced (the renamed model fields + key-map + read-helper signatures; mypy
strict) + test-enforced (the S1 key-round-trip / dual-read / archival-triple / value-source KATs; the
S2 forward-write KATs; the S3 repatch/dry-run/idempotency/no-regression KATs) + prose-enforced (the
empty-not-fallback value rule and the dual-read transition invariant, cited to the Picard convention /
AGENTS.md tag anchor / the MetaBrainz community thread).  **Defined-in:** S1.  **Consumed-by:** S2 (the
forward-write), S3 (the offline repatch), S4 (scan validation), R6d (the one-pass drives the S3 pass
destructively), any future AcoustID consumer.  Over-specified per Category-A: carries the dual-read
transition contract and the value-source rule though S2 is the first consumer.

### Consumed (frozen upstream — invalidation is out of scope for this sub-track)

- **Picard tag convention (AGENTS.md tag-convention anchor + the MetaBrainz community thread,
  `community.metabrainz.org/t/acoustid-id-vs-acoustid-fingerprint/676749`)** — the authority on
  `acoustid_id` (= fingerprint-lookup cluster UUID) and `acoustid_fingerprint` (= raw Chromaprint
  string).  R6c aligns to it; it does not re-open the convention.
- **C-AR (R3b) — the archival identity triple + AccurateRip 4th dimension.**  The fingerprint field is
  part of the triple (`audio_hash` / `acoustid_id` / fingerprint).  R6c renames the fingerprint *key*,
  never the triple's structure or the C-AR AccurateRip fields.  Validate-only — preserve field order
  and the `# 4th dim slots in here` reserved-slot comment.
- **C-PROV / C-MOVE + confirmation-provenance** — move/verify/journal provenance.  S2's write rides
  the existing copy/tag/verify loop unchanged; S3's repatch rides the `enrich`
  re-tag→`_verify_copy`→journal chain; the new `"acoustid-repatched"` entry is appended only after
  verification.  Validate-only — preserve the chain exactly.
- **C-NET-CORE / C-NET-TERM (R1) — the `_net` retrieval core + universal terminal rule.**  The
  `/v2/lookup` and `list_by_mbid` calls already route through `retrieve()` with `_acoustid_classify`.
  S2/S3 add no raw network call and do not alter the retry/terminal posture.  Validate-only.
- **"Path is a handle, not a manifest"** — R6c changes tag *content*/keys, never path structure; no
  `build_dest_path` change, no repath.

### Produced

- **C-ACID** — the Picard-aligned AcoustID tag policy at S1; the forward-write at S2; the offline
  repatch at S3; scan validation at S4.  **Coordinates with R6d** (the destructive library-wide
  repatch): the machinery is landed here; R6d runs the S3 pass destructively under J3 — adding an
  **AcoustID tag-content-repatch capability** to R6d's engine (the same gap R6b closed for the
  catalogue-colon case, now for the AcoustID key + value).

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 @architect | Freeze the Picard-aligned AcoustID tag policy (value source + key rename + dual-read) | pending | | |
| 2 | Align the AcoustID forward-write path to the frozen policy | pending | | |
| 3 | Migrate existing files in an offline AcoustID repatch pass | pending | | |
| 4 ◆ | Scan the library for legacy AcoustID tag state + census + anneal | pending | | |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **D-1 (S1 value-source fallback — the inflection judgment).**  When no api_key / no fingerprint,
  `ACOUSTID_ID` is empty at ingest (empty-not-`list_by_mbid`, per posture 1).  Resolution to freeze at
  S1: the fingerprint `/v2/lookup` cluster UUID is the single source; a `list_by_mbid` fallback would
  re-introduce the dual-source divergence the shard removes.  **Reopen trigger:** if the operator's
  ingest routinely runs keyless, the empty-at-ingest cost is larger than estimated — an
  *additive-reshard* (a `list_by_mbid`-fallback row), decided live; do **not** silently restore the
  fallback in-track.  *internal-continue* pending the S1 freeze.
- **D-2 (persisted-key migration — dual-read is mandatory).**  Existing library files carry the legacy
  `CHROMAPRINT_FP` key; a hard rename (write-new/read-new-only) silently drops the fingerprint on read
  until R6d.  Resolution (posture 2): dual-read (read both keys) frozen in C-ACID; the S3 repatch
  retires the legacy key per file; dual-read removal is a later explicit decision.  *internal-continue.*
- **D-3 (host-path silent-no-op hazard — carried from R6a D-3 / R6b D-3 / R4b D-1).**  The new
  scanner's `ROOT` is machine-specific (`~/Remote/hades/Music/Done`, the `scan_nonuniform_depth.py`
  pattern).  S4 **must** distinguish scan-not-run (unmounted/empty root → never "clean") from
  no-findings.  Operator mounts the library before `/plan-run`; if unmounted at execution, the census
  refresh is recorded pending, not asserted.  *internal-continue* (S4 handles it structurally).
- **D-4 (R6d coupling — sequencing constraint, not a risk).**  This shard builds the machinery; the
  destructive library-wide AcoustID repatch is R6d's one J3-gated pass (D-A5/D-A7).  The S3 pass is the
  machinery R6d drives — adding an AcoustID tag-content-repatch capability to R6d's engine (mirrors R6b
  for the catalogue-colon case).  No destructive op in this sub-track.  *internal-continue.*
- **D-5 (temporary library inconsistency — accepted, D-A4/D-A6-style).**  Until R6d's repatch, the
  on-disk library mixes migrated (new ingests: `ACOUSTID_FINGERPRINT`, fingerprint-sourced
  `ACOUSTID_ID`) and legacy (`CHROMAPRINT_FP`, `list_by_mbid`-sourced `ACOUSTID_ID`) files.  The
  dual-read makes this harmless for reads.  Accepted (posture 3); not a defect to remediate in-track.
  Noted so `/plan-run` does not treat it as an in-track discovery.
- **D-6 (BACKLOG premise correction — recorded, not a risk).**  BACKLOG framed the value problem as
  "track UUID vs cluster UUID"; the survey (2026-08-13) found both writer paths already emit a *cluster*
  UUID (the `list_by_mbid` id is a cluster identifier, `_mb_api.py:1084`), differing only by lookup key.
  So the fix is a value-*source* unification, not a value-*type* fix, and it adds **no new fpcalc
  dependency** (fpcalc + `/v2/lookup` already run at ingest, `_pipeline.py:1217,1226`).  Noted so
  `/plan-run` does not re-derive against the superseded BACKLOG framing.  *internal-continue.*

## Notes for executors

- **Tier routing.**  S1 is **Opus + `@architect` inflection** (the C-ACID value-source + dual-read
  policy; permanent library-wide tag policy; correctness-crit — a wrong fallback re-introduces the
  divergence, an archival identity dimension is at stake).  S2, S3, S4 are **Sonnet** (mechanical over
  the frozen policy, modelled on `enrich` / `repatch_catalogue_colon`).  `juncture-tier: opus` — kept.
- **Register: align to Picard, don't re-open the convention.**  Picard's `acoustid_id` /
  `acoustid_fingerprint` definitions (AGENTS.md tag anchor) are the authority; R6c conforms to them.
- **Empty-not-fallback is load-bearing.**  On no api_key / no fingerprint, leave `ACOUSTID_ID` empty —
  never re-fill from `list_by_mbid`.  Every forward-write test must carry a no-key case asserting the
  empty result (no fallback).
- **Reuse the already-made `/v2/lookup` call.**  Ingest already fetches the cluster UUID at
  `_pipeline.py:1226` and discards it — capture the `[1]` value; do **not** add a second network call.
- **Dual-read is a read-widen, not a write-fork.**  Write only `ACOUSTID_FINGERPRINT`; read both it and
  legacy `CHROMAPRINT_FP`.  A write that emits the legacy key violates the policy.
- **Model S3 on `repatch_catalogue_colon` / `enrich`, not fresh.**  Both are existing offline
  tag-content write passes: idempotent, `dry_run`, re-tag→`_verify_copy`→journal (P-FP3/P-FP4).  Reuse
  that shape (a new `action="acoustid-repatched"` entry appended only after verification).
- **REGISTER rule (durable-file discipline).**  In source/tests, state the *property/reason/invariant*
  — never the plan coordinate.  "the AcoustID cluster UUID from the fingerprint /v2/lookup (Picard's
  `acoustid_id` source)" is right; "the S1 value-source freeze" is not.  Plan vocabulary (S1/S2/S3/S4,
  R6c, sub-track names, `/plan-run`) lives only in `PLAN.md` / `ROADMAP*.md` / the ledger / commit
  messages.  See the repo `AGENTS.md` "Register rule" block.
- **Anneal denylist (◆ gate greps durable files for these).**  Seeded from the `/plan-run` default,
  tuned for this project's vocabulary:
  - `\bS[1-9]\b` (this sub-track's plan session coordinates) — **but** allow STYLEGUIDE-rule-section
    forms (`\b[1-5]\.[0-9]\b` like "4.5", "3.1" are register/rule cites, not plan coordinates — do
    **not** flag).
  - `\bR6[a-e]\b`, `\bR[0-9]\b` (roadmap node coordinates) — flag in durable source/tests; legitimate
    only in PLAN/ROADMAP/ledger/commit messages.
  - `sub-track`, `plan-run`, `plan-shard`, `halt-at-boundaries`, `run-to-boundary`
  - `C-ACID` **only outside docstrings that legitimately name the contract** — contract names in
    docstrings are the intended durable form; flag bare "S1 freeze"-style prose, not the contract name.
  - `juncture`, `inflection`, `action-frame`, `◆`
  - Do **not** add `AcoustID`, `acoustid_id`, `ACOUSTID_ID`, `ACOUSTID_FINGERPRINT`, `CHROMAPRINT_FP`,
    `chromaprint`, `Picard`, `/v2/lookup`, `list_by_mbid`, `cluster UUID`, `fingerprint` to the
    denylist — these are legitimate domain/convention vocabulary this sub-track deliberately renders
    and cites.
- **Invariants to preserve:** the empty-not-fallback value rule + the dual-read transition (C-ACID);
  the Picard convention alignment; the `enrich` / copy-tag-verify confirmation-provenance chain (S2/S3
  ride it unchanged — the `"acoustid-repatched"` entry appended only after verification); C-AR (the
  archival triple structure + AccurateRip fields + the reserved 4th-dim slot — unchanged, only the
  fingerprint *key* renames); C-NET-CORE/C-NET-TERM (no raw network call, retry/terminal posture
  unchanged); "path is a handle, not a manifest" (no path/repath change — tag content only).
- **Every row runs `~/.local/bin/tox -m analyze` before ledger-done** (build + test at 100% branch
  coverage + strict mypy + ruff + pylint 10.00/10 + pyupgrade).  Import order via
  `~/.local/bin/tox -m edit`, never hand-edited.
- **Suggested first `/plan-run` invocation:** `halt-at-boundaries` — the C-ACID value-source policy
  (the empty-not-fallback ruling + the dual-read transition) is the first unproven substrate judgment
  in this shard; stop after S1 for an operator check that the freeze (especially the no-key
  empty-at-ingest behaviour and the dual-read transition) is right before S2 consumes it.  Once S1
  confirms, `run-to-boundary` through the S4 ◆.
