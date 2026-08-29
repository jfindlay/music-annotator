<!-- Rolling action frame.  The previous sub-track (one-time truthful amendment of suppressed cross-references /
     C-AMEND) closed 2026-08-29 with S1 done (commit be68c38); its plan and ledger live in this file's git history.
     C-AMEND's durable narrowing of the "no journal edits" ruling was folded up into docs/NOTES.md at this boundary
     (journal-invariant prose contract: an append recording what the current correct code would have journalled,
     sourced from surviving embedded evidence, is a truthful amendment; edit/reorder/delete/falsify remains a
     forbidden rewrite).  This sub-track shards C-LOCAL-ID: the catalog-gate ingest verb for never-in-MB releases —
     the source-tags-only floor of full inclusion.  Design pinned in docs/NOTES.md "Local accession identity".
     Rewritten at the next boundary. -->

# PLAN — C-LOCAL-ID: catalog-gate ingest verb for never-in-MB releases

## Purpose (design intent)

Full inclusion admits releases with **no external identity, ever** — a self-recorded chamber performance that will
never appear in any datastore.  `source-tags-only` (tier 4) is that release's **permanent** home, not a waiting room.
Today there is no CLI path to it: `apply` requires an MB release MBID (`--release-id`), and `run()` structurally
cannot emit a `source-tags-only` entry (the explicit comment at `_pipeline.py:1925`).  This sub-track builds the
missing verb: an operator supplies a minimally-tagged source directory (standard Vorbis/Picard vocabulary), the verb
validates the required tag set, **mints a local accession UUID** into `MUSICANNOTATOR_RELEASEID`, and flows the files
through the **same** copy→SHA→tag→verify→journal chain at `source-tags-only`.

Integrity is derivational: a track is reachable iff its embedded tags render a legal path through `build_dest_path`,
the journal is rebuildable from tags, every maintenance pass re-derives from tags.  So the "no unreachable tracks"
guarantee for non-MB releases needs no new index — only that the tag inputs the path grammar consumes are present and
valid at cataloging time, enforced at the same chokepoint every MB-sourced release already flows through.  The
accession UUID is the **complementary key**: renderability is not identity, and `audit`/`unify`/`regroup`/`prune` join
on a release ID — a release without one is invisible to the fragmentation/consolidation perimeter.  Archival framing:
the local ID is the *accession number* (permanent, ours); the MBID is the *catalogue raisonné number* (MB's).
Complementary, never competing.

Re-read this section at every ◆ boundary (anti-defocus anchor).

## Verify gate

Discovered from `pyproject.toml` tox config (do not assume `make`):

- **VERIFY_TEST**: `~/.local/bin/tox -e test` — `pytest tests/` with `--cov=music_annotator`, `fail_under = 100`
  (100% branch coverage enforced).
- **VERIFY (full gate)**: `~/.local/bin/tox -m analyze` — `build` + `test` + `check_type` (mypy strict, `src/ tests/`)
  + `check_format` (ruff check + ruff format --check) + `check_lint` (pylint 10.00/10) + `check_upgrade`
  (pyupgrade --py312-plus).  One green run satisfies tests, types, lint, format, coverage.

Every session declares done only on a green `~/.local/bin/tox -m analyze`.

## Session list

| #  | Session (commit-title shaped)                                              | Cat | Tier | Consumes                       | Expected files |
|----|----------------------------------------------------------------------------|-----|------|--------------------------------|----------------|
| S1 | Add MUSICANNOTATOR_RELEASEID accession-identity tag (FLAC + MP3 TXXX)       | A   | Opus | —                              | `src/music_annotator/models.py`, `src/music_annotator/_tagger.py`, `tests/unit/test_models.py` |
| S2 | Validate operator tag set and build renderable TrackTags for local ingest  | B   | Opus | C-LOCAL-ID (namespace)         | `src/music_annotator/_pipeline_local.py` (new), `src/music_annotator/models.py`, `tests/unit/test_pipeline.py` |
| S3 ◆ | Add local-ingest verb: mint accession UUID, journal at source-tags-only   | I   | Opus | C-LOCAL-ID, C-ACCESSION-GATE   | `src/music_annotator/_pipeline_local.py`, `src/music_annotator/__main__.py`, `src/music_annotator/__init__.py`, `tests/unit/test_main.py`, `tests/integration/test_integration.py` |

`juncture-tier: opus` for all rows (ROADMAP standing decision 2026-07-18: provenance-chain work keeps
correctness-criticality high; the adjudicator does not opt down).  No inflection `@architect` juncture is expected —
the substrate interfaces (`_copy_tag_verify_journal_pass`, `classify_annotation_tier`, `_MP3_TXXX_MAP`,
`build_dest_path`) are all already frozen and surveyed; C-LOCAL-ID freezes a *new* tag namespace on top of them, not a
contested substrate redesign.  S3 is the sub-track-final ◆ boundary (the whole sub-track is one deliverable: a working
verb) and carries the integrative anneal.

## Session detail

### S1 — MUSICANNOTATOR_RELEASEID accession-identity tag (Cat A substrate; freezes C-LOCAL-ID)

**Deliverable.** A new `musicannotator_releaseid: str = ""` field on `TrackTags` (`models.py`, near
`musicbrainz_albumid` at ~`:1400`) and a `"MUSICANNOTATOR_RELEASEID": "MusicAnnotator Release Id"` entry in
`_MP3_TXXX_MAP` (`_tagger.py:71`).  FLAC writes it automatically via `to_file_dict()` → `apply_tags_flac`; MP3 writes
it via the TXXX loop keyed on the map; read-back verification (`_read_tags_mp3`, same map) is symmetric by
construction.  This session **over-specifies** the namespace per Category-A discipline: it freezes the tag key, the
`MUSICANNOTATOR_*` namespace convention, and the never-mint-into-`MUSICBRAINZ_ALBUMID` rule as C-LOCAL-ID, even though
no caller writes the tag yet.

**KATs.**
1. **FLAC round-trip** — a `TrackTags` with `musicannotator_releaseid="<uuid>"` written to FLAC then read back yields
   the same value as a lowercase `musicannotator_releaseid` Vorbis comment.
2. **MP3 round-trip** — the same value written to MP3 lands in a TXXX frame (desc `"MusicAnnotator Release Id"`) and
   reads back equal.
3. **Empty is not written** — `musicannotator_releaseid=""` produces no FLAC comment and no MP3 TXXX frame (the tag is
   absent, not empty) — same discipline as every other optional `TrackTags` field.
4. **Namespace independence** — `musicannotator_releaseid` and `musicbrainz_albumid` are independent fields: setting
   one never populates the other (the never-mint rule at the type level).

**Subtleties.** The `TrackTags` model config is `extra="allow"`; the field must be a declared field (not an extra) so
mypy-strict and the `to_file_dict()` serialization treat it as first-class.  Confirm `_MP3_STD_KEYS` is *not* touched
(this is a custom tag, TXXX-only, not a standard ID3 frame).

**Deferrals.** No minting logic, no validation, no verb — S1 is purely the tag substrate.

### S2 — Catalog-gate validation + renderable TrackTags construction (Cat B; freezes C-ACCESSION-GATE)

**Deliverable.** A new module `_pipeline_local.py` (name is executor latitude) holding two pure, testable pieces:
(a) `validate_local_tags(...)` — the catalog gate: reads the embedded tags of the source files (operator pre-tagged
with standard Vorbis/Picard vocabulary), asserts the **minimal required set** is present and valid, raises a precise
error otherwise; (b) a builder that constructs a path-renderable `TrackTags` (and the stub `MBTrack`/`MBRelease`
`build_dest_path` needs — both are unread/API-stability stubs per the survey) directly from the validated embedded
tags, **not** via `build_track_tags` (which requires an MB release and is unusable here).

**Minimal required set (validated non-empty):** `ALBUMARTIST` (the performer-led first path component — without it the
release has no top dir), `ALBUM`, per-track `TITLE`, per-track `TRACKNUMBER` (unique, **contiguous 1..n** — the
leaf-numbering invariant).  `DATE` is **required-with-explicit-unknown**: the operator either supplies a real year or
affirms unknown via an explicit sentinel (a `--date-unknown` flag or a `DATE=unknown` keyword — executor latitude on
surface), which the gate records as an *affirmed* empty DATE (renders no `[rel YYYY]` suffix).  A silently-empty DATE
with no affirmation is a **gate failure**, not an accepted absence — this distinguishes "operator declared unknown"
from "operator forgot" (lossless principle: never fabricate to satisfy a form, but never silently accept a hole
either).  `CWP_*` composer/work fields are **optional-if-genuinely-known**: a self-recorded Schubert quintet
legitimately routes through the composer-led branch on knowledge the operator actually possesses.

**KATs.**
1. **Complete set validates** — a source dir whose embedded tags carry the full required set + a real DATE passes the
   gate and produces one `TrackTags` per track with `musicbrainz_albumid=""` and the operator's values threaded.
2. **Missing required field fails precisely** — omitting `ALBUMARTIST` (or `ALBUM`, `TITLE`, `TRACKNUMBER`) raises an
   error naming the missing field and the offending track; no partial ingest state is produced.
3. **Non-contiguous track numbers fail** — track numbers `{1,2,4}` (gap) or `{1,1,2}` (dup) raise the leaf-numbering
   error; `{1,2,3}` passes.
4. **DATE affirmed-unknown passes with empty DATE** — the sentinel yields a `TrackTags` with `date=""`, and the
   resulting `build_dest_path` renders no `[rel YYYY]` suffix.
5. **DATE silently-missing fails** — no real year and no affirmation raises the DATE gate error.
6. **Renderable path** — the constructed `TrackTags` + stubs feed `build_dest_path` and produce a legal path with the
   performer-led first component from `ALBUMARTIST` (and the composer-led branch when `CWP_*` is supplied).

**Subtleties.** `build_dest_path`'s leaf number falls back `CWP_MOVT_NUM` → `global_track_idx` → `track.position`;
with no work hierarchy `CWP_MOVT_NUM=""`, so the stub `MBTrack.position` must equal the operator's `TRACKNUMBER` and
the copy loop must pass a reliable 1-based `global_track_idx` (S3 threads this).  The `release` param of
`build_dest_path` is unread (API-stability stub) — an empty `MBRelease()` is safe.

**Deferrals.** No UUID minting, no journal write, no CLI — S2 is the pure gate + builder.  Lower-fidelity on the exact
error-type taxonomy (a single `ValueError` with a precise message vs a small exception hierarchy) is executor latitude
resolved against the repo's existing gate-error convention.

### S3 ◆ — Local-ingest verb: mint accession UUID, journal at source-tags-only (Cat I integrative)

**Deliverable.** The public verb.  A new top-level function (`ingest_local(...)` / working name — executor latitude)
in `_pipeline_local.py` that: (1) calls the S2 gate + builder; (2) **mints a single UUIDv4** per release into
`MUSICANNOTATOR_RELEASEID` on every track's `TrackTags` (one release = one accession ID, embedded in every track so
the journal stays rebuildable from tags alone); (3) calls `_copy_tag_verify_journal_pass(...)` directly with
`census_signal=CensusSignal.NOT_IN_MB` and `release_id=<minted-uuid>`, so the existing tier path emits
`annotation_tier="source-tags-only"` (`classify_annotation_tier`, `models.py:123`) and the journal `"tagged"` entry
carries the accession UUID in the `release_id` role.  Plus: the CLI subparser (a new `ingest` verb — `src_dir` +
`dest_dir` positionals, a `--date-unknown` sentinel, `--dry-run`; **no** `--release-id`, **no** `--user-agent-*`,
since no MB call is made — a new `_add_local_ingest_args` helper, `_add_common_args` is not reusable as-is), the
dispatch arm, the `__init__.py` public re-export + `__all__` update, and the integration test.

**KATs.**
1. **End-to-end local ingest** — a pre-tagged source dir (complete required set) ingested through the real verb lands
   at its `build_dest_path` destination with `annotation_tier="source-tags-only"`, `needs_spot_check=False`, a
   `MUSICANNOTATOR_RELEASEID` UUID embedded in every track, and one `"tagged"` journal entry per track whose
   `release_id` equals that UUID.  The full mutagen write-and-read-back path executes (integration test does not patch
   `apply_tags_*`/`_verify_copy`).
2. **One accession UUID per release** — every track in one ingest shares one `MUSICANNOTATOR_RELEASEID`; a second
   independent ingest mints a *different* UUID (never reused or reassigned).
3. **MUSICBRAINZ_ALBUMID stays empty** — no ingested track carries a `MUSICBRAINZ_ALBUMID`; the accession UUID is
   never minted into the MB field (the never-mint provenance rule, end-to-end).
4. **Confirmation-provenance preserved** — the copy→SHA→tag→verify→journal ordering is unchanged: the `"tagged"` entry
   is appended only after `_verify_copy` passes; a verify failure raises and appends no entry (inherits the repo
   AGENTS.md confirmation-provenance invariant through the reused `_copy_tag_verify_journal_pass`).
5. **`--dry-run` writes nothing** — the verb reports the plan (destination paths, minted-UUID intent, per-track tier)
   and touches neither the destination tree nor the journal.
6. **Gate rejection surfaces** — a source dir failing the S2 gate (missing field / non-contiguous / silently-missing
   DATE) exits non-zero with the precise gate error and produces no destination files and no journal entries.

**Integrative anneal (part of the deliverable).** Durable files (source, tests, docstrings, comments, the STYLEGUIDE
if touched, `__init__.py`) state the property/invariant, never a plan coordinate.  Grep the durable diff against the
anneal denylist below; translate any leaked coordinate into standalone prose.  This session is where C-LOCAL-ID and
C-ACCESSION-GATE "get their public form" — the anneal is the same act as publishing the verb.

**Subtleties.** UUID minting uses `uuid.uuid4()`; the ID is a plain `str(uuid)` in the tag.  Thread the 1-based
`global_track_idx` into the copy loop so the leaf-numbering fallback is deterministic (S2 subtlety).  The verb is the
**first** ingest path that constructs `TrackTags` without an MB release — the integration test is the first of its
kind (no existing verb pattern to copy; build the source-file fixtures from the embedded-FLAC/MP3 byte constants
already in `test_integration.py`).

**Deferrals.** No MB-upstream creation, no promotion path (C-TIER monotonic upgrade already handles a later real MBID
arriving — out of scope here); no batch/multi-release ingest (one release per invocation).

## Cross-session contracts

### C-LOCAL-ID — the local accession-identity tag namespace  *(compiler- + prose-enforced; Defined-in S1; Consumed-by S2, S3)*

**Frozen at S1.**  A never-in-MB release's permanent identity is a locally-minted UUIDv4 carried in the
`MUSICANNOTATOR_RELEASEID` tag (Vorbis comment `musicannotator_releaseid`; MP3 TXXX desc `"MusicAnnotator Release
Id"`), embedded in **every** track of the release (so the journal stays rebuildable from tags alone).  Invariants:

- **Never mint into `MUSICBRAINZ_ALBUMID`.**  Minting a local ID into the MB field is a provenance lie:
  tag-confirmation would attest a fake MB identity, a fixed-MBID dereference would 404, and a later real MB entry finds
  its field squatted.  The accession ID and the MB ID are independent `TrackTags` fields.
- **Namespace by provenance, never by inspection.**  Which tag the ID came from (+ the sidecar tier) answers "is this
  an MBID?"; ID-shape sniffing is forbidden.  MB-dereferencing passes gate on the presence of the MB tag, not on the
  `release_id` value.
- **`MUSICANNOTATOR_*` is the local namespace.**  This is its first member; future local-authority tags join here,
  never in the `MUSICBRAINZ_*` namespace.
- **Retained, never reused.**  If a real MBID later arrives (per-release operator election, never automatic), it lands
  in `MUSICBRAINZ_ALBUMID` and the tier promotes under C-TIER's monotonic-upgrade carve-out; the accession ID stays.

### C-ACCESSION-GATE — the catalog-gate required tag set  *(test- + prose-enforced; Defined-in S2; Consumed-by S3)*

**Frozen at S2.**  A never-in-MB release is admissible iff its embedded tags carry a valid **minimal required set**,
validated at the catalog gate before any copy:

- **Required non-empty:** `ALBUMARTIST`, `ALBUM`, per-track `TITLE`, per-track `TRACKNUMBER` (unique, contiguous
  1..n).
- **`DATE` required-with-explicit-unknown:** a real year, or an operator-affirmed unknown (explicit sentinel → empty
  DATE, no `[rel YYYY]` suffix).  A silently-empty DATE is a gate failure — "declared unknown" ≠ "forgot".
- **`CWP_*` optional-if-genuinely-known:** the epistemic criterion applies to the operator — composer/work fields are
  supplied only on knowledge actually possessed; their presence routes the composer-led path branch.
- **The set is standard Vorbis/Picard vocabulary** — no new schema; if MB identity later arrives, these values are a
  seed MB supersedes in place.

The gate's guarantee is the reachability guarantee: a validated release renders a legal `build_dest_path`, so it is
never an unreachable track.

### Consumed frozen (unchanged; any invalidation is a destructive-HALT)

`C-TIER` (the `AnnotationTier` ladder incl. `source-tags-only` + the `CensusSignal` seam incl. `NOT_IN_MB` +
`classify_annotation_tier` + the monotonic-upgrade carve-out — R2); `C-PROV` / `C-MOVE` (copy→SHA→tag→verify→journal
ordering, the confirmation-provenance invariant, repo AGENTS.md); the lossless principle and "path is a handle, not a
manifest" (NOTES prose contracts); `C-UNIVERSAL` (prefix-less performer-led/composer-led first component — the local
release routes through the same `build_dest_path` shape).  The epistemic criterion (NOTES) governs the operator's
`CWP_*` election.

## Progress ledger

| # | Session                                                                   | Status  | Commit | Froze          |
|---|---------------------------------------------------------------------------|---------|--------|----------------|
| S1 | Add MUSICANNOTATOR_RELEASEID accession-identity tag (FLAC + MP3 TXXX)      | done    | 9bd7b4e | C-LOCAL-ID    |
| S2 | Validate operator tag set and build renderable TrackTags for local ingest | done    | 47e8dcd | C-ACCESSION-GATE |
| S3 | Add local-ingest verb: mint accession UUID, journal at source-tags-only   | done    | 2f46717 | — (◆ boundary: still-on-intent 2026-08-29) |

## Action-frame digest

*(none yet)*

## Discoveries & risks

Carried down from the ROADMAP / NOTES design and the substrate survey (phrased for `/plan-run` discovery
adjudication: internal-continue / additive-reshard / destructive-HALT):

- **`run()` cannot emit `source-tags-only` (confirmed substrate fact, survey 2026-08-29).**  The explicit comment at
  `_pipeline.py:1925` states no-MB is not reachable from `run()` — it requires no release_id.  So S3's verb is a
  **new top-level function calling `_copy_tag_verify_journal_pass` directly**, not a `run()` parameterization.  This
  is designed in (S3 deliverable), not a risk; a design that tries to route through `run()` is the wrong shape.
- **Substrate already exists (confirmed, survey):** `AnnotationTier.SOURCE_TAGS_ONLY`, `CensusSignal.NOT_IN_MB`, and
  `classify_annotation_tier`'s `NOT_IN_MB` arm are all wired; `TransactionEntry.release_id` is a bare `str` that
  accepts a UUID; `build_dest_path` reads no `MUSICBRAINZ_ALBUMID`.  Low design-error risk — the sub-track adds a tag
  namespace and a verb on frozen substrate, not a substrate redesign.
- **Leaf-numbering-without-hierarchy is the one sharp corner (inferred).**  With no work hierarchy `CWP_MOVT_NUM=""`,
  so `build_dest_path`'s leaf number falls back to `global_track_idx` / stub `MBTrack.position`.  If a live case
  surfaces where the fallback mis-numbers a local release, that is an **additive-reshard** signal (tighten the S2
  builder's stub construction), not a destructive-HALT — the fallback chain is pre-existing and tested for MB releases.
- **First-of-kind integration test (risk, low).**  No existing verb constructs `TrackTags` without an MB release, so
  S3's integration fixture is novel.  Mitigation: reuse the embedded-FLAC/MP3 byte constants already in
  `test_integration.py`; the mutagen round-trip path is unchanged.

## Notes for executors

- **Tier routing:** all three sessions Opus (`juncture-tier: opus`, ROADMAP standing decision — provenance-critical).
- **Register (PEDAGOGY):** durable files state the property/reason/invariant, never the plan coordinate.  Contract
  names (C-LOCAL-ID, C-ACCESSION-GATE, C-TIER, C-PROV, C-MOVE, C-UNIVERSAL, and the inherited C-* roster) are
  legitimate durable vocabulary; session coordinates (S1/S2/S3), `sub-track`, and `/plan-run` command vocabulary are
  not — they live only in this PLAN, the ROADMAP, the ledger, and commit messages.
- **Invariants to preserve:** the copy→SHA→tag→verify→journal ordering and the confirmation-provenance chain (repo
  AGENTS.md) — S3 reuses `_copy_tag_verify_journal_pass` precisely so these are shared, not re-implemented.  Never
  mint the accession UUID into `MUSICBRAINZ_ALBUMID` (C-LOCAL-ID).  Never silently accept a missing DATE
  (C-ACCESSION-GATE).
- **Anneal denylist** (the ◆ boundary gate greps durable files for these; seeded from the `/plan-run` default, tuned
  for this domain): `\bS[123]\b` (session ids), `sub-track`, `plan-run`, `boundary rewrite`,
  `juncture`/`inflection`/`action-frame`.  Domain-collision carve-outs (do **not** deny): `ingest`, `accession`,
  `release_id`, `source-tags-only`, `NOT_IN_MB` are legitimate durable code/domain vocabulary, not plan coordinates.
- **Patch targets** bind where the name is imported, not where it originates (repo testing convention): patch
  `music_annotator._pipeline_local.<name>` where the verb binds it, not the originating module.
- **Full gate before declaring any session done:** `~/.local/bin/tox -m analyze` (100% branch coverage, mypy strict,
  pylint 10.00/10, ruff, pyupgrade).
- **Suggested first `/plan-run` invocation:** `halt-at-boundaries` — this is an unproven shard pattern (first
  ingest verb without an MB release, first `MUSICANNOTATOR_*` tag), so stop at the S3 ◆ boundary for review rather
  than auto-continuing past it.
