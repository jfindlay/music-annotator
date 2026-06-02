# music-annotator — Plan: Acoustic Fingerprinting & Archival Identity

This plan is **session-sharded** for autonomous execution by `/run-plan` (see
`~/.config/opencode/multi-session-planning.md` and `~/.config/opencode/command/run-plan.md`).
The currency is the *commit-shaped session*: one `@build`/`@general`/`@explore` dispatch producing
one commit, ending with green checks.  `@plan-admin` (T1) drives the mechanical loop and dispatches
`@committer`; `@plan-deep` (T0) is paged only at inflection points, discovery adjudication, and
sub-track boundaries.  State lives in the Progress ledger and Action-frame digest, not in context.

This is an **independent** sharded plan with its own ledger, parallel to `docs/PLAN.md` (multi-medium
paths) and the dir/file-naming plan.  It consumes nothing from those plans except one soft
dependency noted at F5 (medium-sequence corroboration is *stronger* once the multi-medium substrate
S0 lands, but does not require it).

---

## Purpose (design intent)

Give music-annotator a coherent **acoustic-identity** capability that serves two axes uniformly,
regardless of where a release came from (ripped disc, PrestoMusic download, FreeDB-transferred
library):

1. **Ingest identification** — confirm, and where possible *discover*, the identity of incoming
   releases/media/tracks from their audio, not just their filenames or a user-supplied MBID.
2. **Library integrity** — detect bad MB/AcoustID data, mistagged/misnamed files, wrong pressings,
   and silent audio corruption across the already-annotated library.

Both axes ride one **identity substrate**: a per-track resolution ladder, cheapest signal first,
degrading gracefully when a signal is unavailable (no network, no `fpcalc`, no API key, no AcoustID
coverage).

### The archival identity triple (the central data model)

Every track carries three identity values, stored in **both** the embedded tag (travels with the
file; present-state authority) **and** the journal (cheap detector; goes stale on move).  The three
are complementary, not redundant — exact / fuzzy-acoustic / identity-cluster:

| Value           | What it is                              | Generator         | Tagging-invariant | Role                                            | Stability                    |
|-----------------|-----------------------------------------|-------------------|-------------------|-------------------------------------------------|------------------------------|
| `audio_sha256`  | exact hash of the *decoded audio only*  | mutagen / hashlib | **yes**           | exact integrity; "is the audio byte-stable"     | **anchor** — only a re-rip changes it |
| `chromaprint_fp`| Chromaprint acoustic fingerprint string | `fpcalc`          | yes (audio-only)  | fuzzy similarity (collision, offline integrity) | **floats** — correctable     |
| `acoustid_id`   | AcoustID cluster-ID (UUID) label        | AcoustID service  | yes               | identity grouping (already a tag today)         | **floats** — correctable     |

**Planned fourth dimension — `accuraterip` (rip-fidelity, not in this plan's scope).**  A future
archival field will carry whipper's AccurateRip result, making the triple a **quartet**.  It is a
genuinely *distinct* axis from the three above: the triple answers "what is this / is the audio
stable"; AccurateRip answers "**was this rip done correctly**" — bit-fidelity of the rip against a
crowd consensus of the same pressing.  It is orthogonal to `audio_sha256` (a *local* invariant, no
crowd), to `chromaprint_fp` (acoustic similarity, not bit-fidelity), and to `acoustid_id`
(identity).  It is **not** a rung on the identity ladder — it is a provenance/integrity dimension the
identity rungs cannot express.  It is deferred here for two reasons: (1) it is produced *during
ripping* and depends on a **whipper ingest mode** that does not yet exist in any plan
(music-annotator would *read* whipper's AccurateRip result as rung-0 provenance, or much later
compute it); (2) **P-FP3 already makes adding it free** — because retroactive enrichment is built as
re-runnable maintenance (`audit --enrich`) rather than a one-time migration, a 4th archival field
gets backfilled over the existing library by the same permanent tool.  The architecture is shaped to
receive it; this plan only names it and reserves the framing.

### Two framings that govern the whole featureset (anti-defocus checks)

- **Hash anchors, identity floats.**  `audio_sha256` is ground truth — invariant to tagging, derived
  from the audio itself; the *only* legitimate way it changes is a re-rip or a cleaner-source
  replacement, so a *changed* audio hash is a meaningful event, never noise.  `chromaprint_fp` and
  `acoustid_id` are *fallible derived claims* (AcoustID especially — crowd-sourced, improves over
  time).  All identity machinery must treat them as **correctable**: re-derivable, overwritable,
  never trusted as immutable.  The audit pass must be able to *update* a stale/wrong `acoustid_id`
  while leaving `audio_sha256` as the proof it is still the same audio.

- **Generation vs resolution (the AcoustID/Chromaprint distinction).**  Chromaprint (`fpcalc`)
  *generates* a fingerprint from audio.  AcoustID is an online service *built on* Chromaprint that
  *resolves* a fingerprint to an identity.  They are a pipeline, never alternatives.  "Use both
  defensively" means **graceful fallback down the rung ladder**, not redundant providers.  There is
  **no surviving use for Chromaprint-exact** equality: exact-identity is `audio_sha256`'s job
  (cheaper, no binary), fuzzy-similarity is Chromaprint-Hamming's job; exact-Chromaprint occupies a
  useless dominated middle and the existing `src_fp == dest_fp` collision check is *replaced*, not
  extended.

### Backward compatibility is a first-class requirement, not a footnote

The existing partially-annotated library predates this featureset: those files have no
`audio_sha256` tag, no `chromaprint_fp` tag, possibly a missing or *wrong* `acoustid_id`.  Any
feature that does not have a **retroactive enrichment path** bifurcates the library into
"annotated-before-fingerprinting" and "annotated-after" — exactly the silent inconsistency this
featureset exists to prevent.  The resolution (F4): build retroactive enrichment **as an idempotent,
re-runnable maintenance capability from the start** (`audit --enrich`), not a throwaway migration.
This serves the transient need (enrich today's library) via the permanent tool (keep the library
self-consistent as the schema grows).  It must be tolerant of pre-existing *wrong* `acoustid_id`
tags and able to correct them.

**Re-read this section at every ◆ sub-track boundary** to verify the work still tracks the intent.

### The rung ladder (substrate; cheapest signal first)

| Rung | Signal                                            | Cost      | Network | Binary  | Answers                                         |
|------|---------------------------------------------------|-----------|---------|---------|-------------------------------------------------|
| 0    | embedded tags on source (MBID / ISRC / AcoustID)  | trivial   | none    | none    | "what does the file *claim* to be?"             |
| 1    | ISRC ↔ `MBRecording.isrc_list`                    | cheap     | MB only | none    | identity for commercial sources (PrestoMusic)   |
| 2    | AcoustID UUID equality (`fetch_acoustid_id`)      | cheap     | keyless | none    | cluster-equality verification (exists today)    |
| 3    | `audio_sha256` exact match                        | trivial   | none    | none    | exact integrity / collision over time           |
| 4    | Chromaprint-fuzzy (Hamming distance)              | ~5–10s/tk | none    | fpcalc  | "same recording, different encode" (collision)  |
| 5    | Chromaprint → AcoustID `/v2/lookup` (audio→MBID)  | ~5–10s/tk | **keyed** | fpcalc | *identification* from raw audio                 |
| 6    | duration (existing)                               | trivial   | none    | none    | last-resort weak signal                         |

Rungs 0–4 are offline-capable (no API key).  Rung 5 is the only keyed/online-fragile rung and is
isolated to the ingest axis behind `--acoustid-key`.

---

## Session list

One row = one dispatch = one commit.  `Cat` = category (A substrate / B algorithm / C consumer /
X context-substance).  `T` = tier: O = Opus inflection (orchestrator designs inline, then HALT for
sign-off); S = Sonnet `@build`.  ◆ marks the last session of a sub-track.  `Dep` lists the
session-numbers / frozen contracts a row depends on.  This table is intentionally wider than the
128-char rule — tables don't wrap, and the data is the point.

| #  | Title (commit-shaped)                                      | Cat | T | Dep        | Expected files                                                                 | KAT |
|----|------------------------------------------------------------|-----|---|------------|--------------------------------------------------------------------------------|-----|
| F0 | Archival-identity substrate: triple fields + audio hash    | A   | O | —          | `models.py`, `_pipeline_io.py`, `_tagger.py`, `tests/unit/test_pipeline.py`     | `test_audio_hash_invariant_across_tagging` |
| F1 | Compute + write `audio_hash` at ingest (tag + journal)     | C   | S | F0         | `_pipeline.py`, `_pipeline_io.py`, `tests/unit/test_pipeline.py`                | `test_ingest_writes_audio_hash_tag_and_journal` |
| F2 | ISRC rung: populate `isrc`, write tag, ISRC↔isrc_list match ◆ | B | S | F0         | `_tags.py`, `models.py`, `_pipeline_io.py`, `tests/unit/test_pipeline.py`       | `test_isrc_match_resolves_identity` |
| F3 | Chromaprint fp at ingest + replace exact collision w/ fuzzy | B  | S | F0         | `_pipeline_io.py`, `_pipeline.py`, `tests/unit/test_pipeline.py`                | `test_chromaprint_fuzzy_same_recording_different_encode` |
| F4 | `audit --enrich`: idempotent retroactive backfill ◆        | C   | O | F1,F2,F3   | `__main__.py`, `_pipeline_io.py`, `tests/unit/test_main.py`                     | `test_enrich_backfills_triple_idempotently` |
| F5 | Medium-sequence corroboration over per-track identity ◆    | B   | S | F0         | `_pipeline_io.py`, `_discover.py`, `tests/unit/test_discover.py`                | `test_medium_sequence_corroborates_weak_track` |
| F6 | Keyed audio→MBID: `fetch_acoustid_lookup` + `--acoustid-key` | C | O | F0,F3      | `_mb_api.py`, `__main__.py`, `_discover.py`, `tests/unit/test_mb_helpers.py`    | `test_acoustid_lookup_seeds_release_search` |
| F7 | Integrity `audit`: detect→adjudicate→confirm ◆             | C   | S | F4,F1      | `__main__.py`, `_pipeline_io.py`, `tests/unit/test_main.py`                     | `test_audit_flags_wrong_acoustid_keeps_audio_anchor` |
| F8 | Integrative writeup + NOTES invariants + §307 fold-in ◆    | X   | O | F1-F7      | `docs/NOTES.md`, `README.md`                                                    | — (prose) |

### Sub-track boundaries

- **◆ Sub-track A — archival identity at ingest** ends at F3.  Ships: the triple stored in tag +
  journal for every *newly* ingested track (`audio_hash`, `chromaprint_fp`, `acoustid_id`), ISRC
  as an identity rung, and fuzzy-Chromaprint collision replacing exact equality.
- **◆ Sub-track B — retroactive enrichment** is F4 (single session, the backward-compat spine).
  Ships: idempotent `audit --enrich` that backfills the triple over the already-annotated library
  and can *correct* a stale/wrong `acoustid_id`.
- **◆ Sub-track C — sequence + identification** ends at F6.  Ships: medium-sequence corroboration
  (F5, offline) and the keyed audio→MBID identification path (F6, behind `--acoustid-key`).  F5 and
  F6 are independent and may execute in either order.
- **◆ Sub-track D — integrity audit** is F7.  Ships: the read-only `audit` pass that detects
  fragmentation/mistag/wrong-pressing using journal-detects → tag-adjudicates → audio-anchor-confirms.
- **F8** is the integrative capstone: names the new prose invariants and folds in the long-deferred
  old `PLAN.md §307` `--verify-fingerprints` source-check (now subsumed by rung 5).

### Notes per session

- **F0 (Opus inflection — HALT for sign-off before dispatch).**  The substrate.  Three pieces, one
  commit:
  1. Add `audio_hash: str = ""` and `chromaprint_fp: str = ""` to `TrackTags` (`models.py`, after
     the `acoustid_id` field).  Both written to the output file (do **not** add to the
     `to_file_dict` exclusion set).  FLAC keys lowercase to `audio_hash` / `chromaprint_fp`; MP3
     TXXX mappings `"AUDIO_HASH": "Audio Hash"` and `"CHROMAPRINT_FP": "Chromaprint Fingerprint"`
     added to `_MP3_TXXX_MAP`.  *(Implemented; field renamed from `audio_sha256` at F0 sign-off —
     see C-F0c and the sign-off resolution note in Cross-session contracts.)*
  2. Add full archival triple to `TransactionEntry` (`models.py`): `audio_hash`, `chromaprint_fp`,
     `acoustid_id` — all `str = ""`, additive, backward-compatible.  *(Implemented.)*
  3. Define the **audio-payload hash primitive** in `_pipeline_io.py`: `_audio_hash(path) -> str`
     (renamed from `_audio_sha256` at sign-off).  Algorithm-tagged, decode-free: FLAC →
     `"flac-md5:<32hex>"` from STREAMINFO; MP3 → `"mp3-stream-sha256:<64hex>"` over audio frames
     post-ID3.  The invariance is the contract; the KAT (`test_audio_hash_invariant_across_tagging`)
     locks it: tag a file, re-read, assert `audio_hash` unchanged before vs after `apply_tags_*`.
  4. Generalise the existing `AudioCompareResult` (`_pipeline_io.py:57`) into the identity-result
     shape the ladder needs, OR introduce a sibling `IdentityResult` — decide at sign-off; do
     **not** over-build a class hierarchy.  **This is the contract every later session consumes —
     over-specify the field surface here.**
  5. **Reserve room for the 4th dimension.**  Design the field set, the journal schema, and the
     enrichment-shaped result so that adding `accuraterip` (whipper) later (see Purpose "Planned
     fourth dimension") is additive, not a re-shard — e.g. group the archival fields so a 4th slots
     in without renaming or restructuring.  Do **not** add `accuraterip` now (no whipper mode
     exists); just do not design it *out*.
- **F1.**  In `run()`'s copy/tag/verify loop, compute `_audio_hash(src_file)` and store it on
  `final_tags.audio_hash` *before* `apply_tags_*` writes it to the file, and carry it into the
  `action="tagged"` journal entry.  Preserve the journal provenance chain (AGENTS.md invariant):
  the hash is captured pre-tag but the journal entry is still appended only after `_verify_copy`
  succeeds.  No new network call.
- **F2.**  Populate `TrackTags.isrc` (`models.py:1024`, currently never set) from
  `MBRecording.isrc_list` (`models.py:745`, fetched but unused) in `build_track_tags`/`build_*_tags`
  (`_tags.py`); write it as a tag.  Add an ISRC rung to the identity resolver: when a source file
  carries an ISRC tag (rung 0 read) matching a candidate recording's `isrc_list`, that is a
  definitive offline identity signal.  Most useful for PrestoMusic downloads, which always carry
  ISRCs.  No binary, MB-only network.
- **F3.**  Two pieces: (a) compute `chromaprint_fp` via `_run_fpcalc` at ingest and store it on
  `final_tags.chromaprint_fp` + tag + journal (mirrors F1); (b) **replace** the exact
  `src_fp == dest_fp` check in `_compare_chromaprint_and_duration` (`_pipeline_io.py:216`) with a
  Hamming-distance fuzzy comparison over the decoded fingerprint bitstrings, with an explicit
  similarity threshold (decide and document the threshold; ~%-similarity that distinguishes
  same-recording-different-encode from different-recording).  This is the *only* collision use of
  Chromaprint; integrity rests on `audio_sha256` + `acoustid_id`, not fuzzy-Chromaprint.  KAT
  proves two different encodes of the same recording now match where exact equality failed.
- **F4 (Opus inflection — HALT for sign-off before dispatch).**  The backward-compat spine.  Build
  `audit --enrich <dest_dir>` as an **idempotent, re-runnable** maintenance mode (not a throwaway
  migration): walk the journal's `action == "tagged"` destinations, and for each file missing any of
  the archival triple, compute and backfill it into *both* the tag and the journal entry.  Crucially
  **tolerant of pre-existing wrong values**: an existing `acoustid_id` may be wrong and must be
  *correctable* — offer a re-resolve that updates `acoustid_id`/`chromaprint_fp` while leaving
  `audio_sha256` as the anchor.  Idempotent: a second run over an already-enriched library is a
  no-op (KAT asserts this).  Must append its own journal updates (NOTES "journal detects, tag
  adjudicates" corollary — maintenance that mutates must re-journal).  This is the first concrete
  operation of the `audit` subcommand; F7 adds the read-only detection modes.
- **F5.**  Medium-sequence corroboration sits *above* the per-track ladder.  The corroboration unit
  is the **medium's ordered track sequence**, not the single work: a medium holds multiple multitrack
  works in sequence, a multitrack work can span media, and **until the whole medium is identified we
  cannot confidently place work boundaries between tracks** — so the whole-medium ordered sequence is
  the stable, defensible unit.  Accept a joint identity hypothesis only when the source files'
  per-track resolutions form the same ordered sequence on a candidate release's medium (or a
  contiguous cross-medium span).  This is what rescues weak/short-track fingerprints (a 25 s chant
  verse is never judged alone).  **Soft dependency:** the cross-medium-span generalisation is
  *stronger* once `docs/PLAN.md` S0 (multi-medium substrate) lands, but the medium-scoped case does
  not require it — implement medium-scoped now, note the cross-medium extension as a Discovery.
- **F6 (Opus inflection — HALT for sign-off before dispatch).**  The only keyed/online rung.  New
  `fetch_acoustid_lookup(fingerprint, duration_s, api_key) -> list[str]` in `_mb_api.py` hitting
  `https://api.acoustid.org/v2/lookup` (distinct from the keyless `list_by_mbid` endpoint
  `fetch_acoustid_id` already uses at `_mb_api.py:833`).  Two-layer retry posture per AGENTS.md
  "Defensive download posture" (`@_mb_retry`-style: retry 5xx, fast-fail 4xx, 1 req/s polite delay).
  Key supplied via CLI flag `--acoustid-key` (no persisted config); when absent, rung 5 degrades to
  *inconclusive* exactly as `fpcalc` absence does — never blocks.  Wire into `discover` as a
  Priority-0 search seed (audio→candidate MBIDs→releases) and as an identity-confirm in `run()`.
  Incomplete AcoustID coverage (common for classical) is *inconclusive*, never disconfirming.
- **F7.**  Read-only `audit <dest_dir>` detection modes (the `--enrich` write mode shipped in F4).
  Journal *detects* (cheap scan of `action == "tagged"`), tag *adjudicates* (read
  `MUSICBRAINZ_ALBUMID` / `ACOUSTID_ID` / the triple back), audio-hash *anchors* (re-compute
  `audio_sha256`, compare to stored — a change means re-rip/replacement, a match means the audio is
  stable so any identity discrepancy is a *tagging* error not an audio one).  Report: wrong/stale
  `acoustid_id` (audio anchor matches but cluster disagrees), audio drift (anchor changed),
  mistag/misname.  Read-only — no moves; corrections route through `audit --enrich`.
- **F8.**  Name the new prose invariants in `docs/NOTES.md` (the archival identity triple;
  hash-anchors-identity-floats; generation-vs-resolution / no-Chromaprint-exact;
  backward-compat-via-idempotent-maintenance).  Update `README.md` for the `audit` subcommand and
  `--acoustid-key`.  Fold in / retire the old `PLAN.md §307` `--verify-fingerprints` source-check —
  it is subsumed by rung 5 (F6); record the disposition.

---

## Cross-session contracts

The scaffolding that makes the sessions compose.  A contract is **frozen** once the session that
establishes it is `done` (see the ledger); later sessions consume it and must not break it.

### Compiler-enforced (interfaces / signatures / model fields)

> **F0 sign-off resolution (Opus inflection, juncture 1).**  The four open questions are resolved
> below and baked into the specs.  **One override requires human sign-off** before `@build` is
> dispatched: the archival hash field is renamed `audio_sha256` → **`audio_hash`** and stores an
> **algorithm-tagged value** (`<algo>:<digest>`), because a uniform decoded-PCM SHA-256 (the name
> `audio_sha256` presumes) would require a new heavyweight PCM-decoder binary — violating the
> rung-3 "Binary: none" substrate constraint.  The plan's F0-open note pre-authorised exactly this
> rename ("if (a) is chosen, rename to `audio_hash`").  The KAT renames in lockstep:
> `test_audio_sha256_invariant_across_tagging` → **`test_audio_hash_invariant_across_tagging`**.
> Decisions #1 (full journal triple), #3 (generalise `AudioCompareResult` in place, no sibling
> class), and #4 (algorithm-tagged value + contiguous additive field group reserves the 4th
> dimension) are within the inflection mandate and need no separate sign-off.

- **C-F0a — archival identity tag fields (FROZEN BY F0).**  `TrackTags` gains two new `str = ""`
  fields, placed contiguously immediately after the existing `acoustid_id` field (`models.py:1231`)
  under a demarcating comment `# --- archival identity (extensible: 4th dim slots in here) ---`:
    - `audio_hash: str = ""`  — algorithm-tagged decoded-audio hash; format `"<algo>:<hexdigest>"`
      (see C-F0c for the two `<algo>` values).  **Not** `audio_sha256` (see sign-off resolution).
    - `chromaprint_fp: str = ""` — Chromaprint fingerprint string (populated F3).
  `acoustid_id` already exists.  All three are written to the output file (do **not** add to the
  `to_file_dict` exclusion set at `models.py:1255`).  Tag keys:
    - **FLAC** (lowercase Vorbis keys, written automatically by `apply_tags_flac`'s
      `audio[key.lower()]` loop): `audio_hash`, `chromaprint_fp`, `acoustid_id`.
    - **MP3** — add two entries to `_MP3_TXXX_MAP` (`_tagger.py:115`, beside the existing
      `"ACOUSTID_ID": "Acoustid Id"`): `"AUDIO_HASH": "Audio Hash"` and
      `"CHROMAPRINT_FP": "Chromaprint Fingerprint"`.  (`ACOUSTID_ID` TXXX already mapped.)  These
      add two rows to the writable set used by `_verify_copy`, so the tag round-trip auto-covers
      them — no `_read_tags_mp3` change needed (it inverts `_MP3_TXXX_MAP`).
  Consumed by F1 (writes `audio_hash`), F2 (`isrc`), F3 (`chromaprint_fp`), F4 (enrich), F7 (audit).
  **Additive only — never rename or reorder this group; a 4th field (`accuraterip`) appends here.**
- **C-F0b — journal archival triple (FROZEN BY F0).**  `TransactionEntry` (`models.py:1495`) gains
  the **full triple**, all `str = ""`, additive and backward-compatible (old journals read missing
  fields as `""`), placed contiguously under the same `# --- archival identity (extensible) ---`
  comment after the existing `action` field:
    - `audio_hash: str = ""`
    - `chromaprint_fp: str = ""`
    - `acoustid_id: str = ""`
  Decision #1 resolved **in favour of the full triple** (not `audio_hash` alone): F7's
  "journal detects, tag adjudicates" (P-FP4) needs the journal rich enough to detect identity drift
  without opening every file; the cost is purely additive-defaulted-string sync at the F1/F3/F4
  write sites.  Write site is the existing `action="tagged"` append (`_pipeline.py:1373`); the
  provenance chain (AGENTS.md) is preserved — values are captured pre-tag but the entry is appended
  only after `_verify_copy` succeeds.  Consumed by F1, F3, F4, F7.  **Additive only; 4th field
  (`accuraterip`) appends to this group too.**
- **C-F0c — `_audio_hash(path) -> str` audio-payload primitive (FROZEN BY F0).**  New private
  function in `_pipeline_io.py`.  Returns an **algorithm-tagged** decoded-audio hash, decode-free
  (no new binary — honours the rung-3 "Binary: none" constraint), tagging-invariant:
    - **FLAC** → `f"flac-md5:{FLAC(str(path)).info.md5_signature:032x}"`.  This is the encoder's
      native STREAMINFO decoded-audio MD5 — *definitionally* the decoded-audio hash, written at rip
      time, invariant to every metadata operation (empirically confirmed: mutagen preserves it
      across `save()`).  Rendered as a 32-char zero-padded lowercase hex string.  **Constraint:** a
      FLAC whose STREAMINFO MD5 is all-zero (some rippers omit it) yields
      `flac-md5:00000000000000000000000000000000`; treat all-zero as "encoder did not record an
      audio MD5" — still a stable value to store, but F7/F5 must not treat the zero-MD5 collision of
      two such files as an audio match (document; do not special-case in F0).
    - **MP3** → `f"mp3-stream-sha256:{sha256(<audio-frame bytes>).hexdigest()}"`, where the
      audio-frame byte range is everything from the first MPEG frame (use mutagen `MP3(...)` /
      `audio.info` or the ID3 header size `id3.size` to locate the start) to EOF, **excluding** the
      leading ID3v2 tag and any trailing ID3v1/APE tag.  Decode-free; SHA-256 over the raw MPEG
      audio frames.  Must be robust to ID3v2 size changes (compute the boundary on each read, never
      hardcode).
    - **Unsupported suffix** → `""` (best-effort, like `_read_acoustid_tag`).
  **The invariance is the contract:** the value computed after `apply_tags_*` must byte-equal the
  value computed before.  Any change making it metadata-dependent breaks every downstream session.
  Named tradeoff: FLAC and MP3 hashes are **not cross-comparable** (different algorithms) — the
  algorithm tag makes this explicit and prevents a silent FLAC-MD5 ↔ MP3-SHA collision.  Equality
  is meaningful only within a format; cross-encode similarity is the chromaprint/acoustid rungs'
  job, not this one.  Consumed by F1, F4, F7.  KAT: `test_audio_hash_invariant_across_tagging`
  (tag a FLAC and an MP3, re-read, assert `audio_hash` unchanged before vs after `apply_tags_*`;
  exercise **both** format arms).
- **C-F0d — identity-result shape (FROZEN BY F0).**  Decision #3 resolved: **generalise the
  existing `AudioCompareResult` in place** (`_pipeline_io.py:57`); do **not** introduce a sibling
  `IdentityResult` (two near-identical dataclasses *is* the hierarchy the plan forbids).  Changes:
    - Keep the dataclass name `AudioCompareResult` and its fields unchanged:
      `src: Path`, `dest: Path`, `match: bool | None`, `method: str`, `detail: str`.  (No rename —
      avoids churning F2–F7 imports; widen the docstring to note it now carries identity-rung
      results, not only collision results.)
    - `method` stays a **`str`** (not an Enum): existing tests assert string literals
      (`result.method == "sha256"` etc.) and existing construction sites pass `method="..."`; an
      Enum would break 30+ assertions for no gain.
    - Freeze the closed value set as a module-level constant in `_pipeline_io.py`:
      `_IDENTITY_METHODS: frozenset[str] = frozenset({"sha256", "acoustid", "chromaprint",
      "duration", "unknown", "isrc", "audio_hash"})`.  Existing five + two new rungs (`"isrc"` for
      F2's ISRC↔isrc_list match, `"audio_hash"` for F3's exact-integrity rung).  This documents and
      (optionally) validates the surface F2/F3/F5/F7 consume without an invasive Enum migration.
  Consumed by F2 (`"isrc"`), F3 (`"audio_hash"`; replaces exact-`"chromaprint"` arm with fuzzy),
  F5, F7.
- **C-F6 — `fetch_acoustid_lookup` signature + `--acoustid-key` flag (FROZEN BY F6).**
  `fetch_acoustid_lookup(fingerprint, duration_s, api_key) -> list[str]`; absent key →
  inconclusive, never raises.  Consumed by F8 (§307 fold-in).

### Test-enforced (KATs — grow monotonically)

Each row's KAT (session-list table) must be present and green at every subsequent session.  The
load-bearing regression guard is `test_audio_hash_invariant_across_tagging` (F0, renamed from
`test_audio_sha256_invariant_across_tagging` at F0 sign-off — see C-F0c): any later session that
makes the audio hash metadata-dependent breaks it.  It must exercise **both** the FLAC
(`flac-md5:`) and MP3 (`mp3-stream-sha256:`) arms.  The existing AcoustID/fpcalc
collision tests (`TestCompareAudioCollision` family, `test_pipeline.py:5490+`) **must continue to
pass** — F3 *replaces* the exact-Chromaprint arm; update those tests rather than regressing the
ladder's other layers.

### Prose-enforced (invariants — named, nothing auto-enforces)

- **P-FP1 — Hash anchors, identity floats.**  `audio_hash` is ground truth (re-rip-only change);
  `chromaprint_fp` / `acoustid_id` are correctable derived claims.  Consumed by F4 (must correct
  wrong values) and F7 (anchor distinguishes audio drift from tag error).
- **P-FP2 — Generation vs resolution; no Chromaprint-exact.**  Chromaprint generates, AcoustID
  resolves; exact-identity is `audio_hash`'s job, fuzzy-similarity is Hamming-Chromaprint's.
  Consumed by F3 (replace exact with fuzzy).
- **P-FP3 — Backward compatibility via idempotent maintenance.**  Every archival-field addition needs
  a re-runnable enrichment path over the already-annotated library; no throwaway migrations.
  Consumed by F4 (the spine) and any future archival-field session.
- **P-FP4 — Journal detects, tag adjudicates** (`NOTES.md`).  Triple lives in both; tag is
  present-state authority, journal is detector.  Maintenance that mutates must re-journal.  Consumed
  by F4, F7.
- **P-FP5 — Defensive download posture** (AGENTS.md).  Rung 5 (`fetch_acoustid_lookup`) follows the
  two-layer retry pattern; absent key/coverage is *inconclusive*, never a hard fail or silent
  substitution.  Consumed by F6.
- **P-FP6 — Single-medium copy semantics preserved** (`docs/PLAN.md` P3).  F1/F3 add identity
  capture inside the existing one-medium-per-`run()` loop; they do not widen the copy scope.

---

## Progress ledger

Source of truth for resuming the chain cold.  `/run-plan` updates this on each successful commit.

| #  | Status   | Commit    | Froze / widened          | Notes |
|----|----------|-----------|--------------------------|-------|
| F0 | done     | `166a316` | C-F0a,C-F0b,C-F0c,C-F0d  | audio_sha256→audio_hash rename approved at sign-off; KAT renamed to test_audio_hash_invariant_across_tagging |
| F1 | done     | `a095b63` | —                        | consumes C-F0a/b/c |
| F2 | done     | `75a7357` | — (◆ sub-track A)        | consumes C-F0a/d |
| F3 | pending  | —         | — (◆ sub-track A)        | consumes C-F0a/c/d; replaces exact-Chromaprint |
| F4 | pending  | —         | — (◆ sub-track B)        | Opus inflection — HALT; consumes F1,F2,F3; backward-compat spine |
| F5 | pending  | —         | — (◆ sub-track C)        | consumes C-F0d; soft-dep on PLAN.md S0 for cross-medium span |
| F6 | pending  | —         | C-F6 (◆ sub-track C)     | Opus inflection — HALT; consumes C-F0d, F3; only keyed rung |
| F7 | pending  | —         | — (◆ sub-track D)        | consumes F4,F1 |
| F8 | pending  | —         | — (◆ capstone)           | Opus writeup; consumes F1-F7; folds in old §307 |

**Frozen contracts:** C-F0a (TrackTags archival triple tag fields), C-F0b (TransactionEntry archival triple), C-F0c (_audio_hash primitive — algorithm-tagged, decode-free), C-F0d (AudioCompareResult + _IDENTITY_METHODS)

---

## Discoveries & risks

Action-frame discoveries that update the static-frame roadmap.  Append during execution; evaluate at
sub-track boundaries.

- **PRECONDITION — no Makefile (blocks an unmodified `/run-plan` run).**  This project drives
  everything through `tox` (`~/.local/bin/tox -m analyze`), which should be used instead..  Harder
  bar applies: **100% branch coverage** and **pylint 10.00/10** — every new branch (including `case
  _: # pragma: no cover` arms) needs a test.
- **F0-open — journal triple breadth.**  Decide at F0 sign-off whether the journal stores only
  `audio_sha256` or the full triple.  The tag is the authority regardless; the journal question is
  detector richness vs schema churn.  Recommendation: store the full triple in the journal too —
  scholarly/archival, practically free, and lets `audit` detect identity drift without opening every
  file.
- **F0-open — audio-payload hash representation.**  FLAC exposes a native decoded-audio MD5
  (`STREAMINFO.md5_signature`); MP3 has no native equivalent (hash audio frames post-ID3).  Decide
  whether to (a) reuse FLAC's native MD5 directly (fast, no decode, but MD5 not SHA-256 and
  format-asymmetric) or (b) compute a uniform SHA-256 over decoded PCM for both formats (slower,
  symmetric, requires decode).  The field name `audio_sha256` presumes (b); if (a) is chosen, rename
  to `audio_hash` and document the per-format algorithm.  **This decision is load-bearing for the
  KAT and the F4 enrichment** — resolve at F0 sign-off.
- **F3-open — Chromaprint similarity threshold.**  The Hamming-distance threshold that separates
  "same recording, different encode" from "different recording" must be chosen and documented (and
  ideally sourced from AcoustID/Chromaprint's own clustering heuristics).  Too loose → false
  collision-matches; too tight → reverts to the exact-equality failure mode.  Risk: short tracks
  (<~30 s of audio) yield low-confidence fingerprints — F5 medium-sequence corroboration is the
  mitigation, not a per-track threshold tweak.
- **PLANNED 4th DIMENSION — AccurateRip (whipper).**  Promoted from a discovery hook to a named,
  intended future dimension (see "Planned fourth dimension" in Purpose).  Whipper's AccurateRip
  verifies a *rip is bit-accurate against a crowd consensus of the same pressing* — rip-fidelity, not
  identity — and is orthogonal to all three identity values.  Intended path: a future **whipper
  ingest mode** (not yet in any plan) supplies the AccurateRip result; music-annotator reads it as
  rung-0 provenance and stores it as a 4th archival field, backfilled over the existing library by
  the same `audit --enrich` maintenance per P-FP3.  Out of scope for F0–F8; reserved so the F0
  substrate and F4 enrichment are designed to *receive* a 4th field without rework.  **Backlog hook:
  whipper ingest mode** — the source-adapter work that produces/exposes AccurateRip (and likely a
  proper MB disc-ID from whipper's TOC) belongs in a separate source-adapters plan, not here.
- **DISCOVERY — `MBRecording.isrc_list` is fetched but unused; `TrackTags.isrc` exists but is never
  populated.**  F2 activates both.  Confirmed by reconnaissance: `models.py:745` / `models.py:1024`.
- **RISK — provenance-blindness at ingest.**  Nothing currently reads embedded tags off *source*
  files (`_read_duration_ms` reads audio metadata only).  Rung 0 (read source MBID/ISRC/AcoustID
  tags) is new surface; F2 introduces the first source-tag read and must be best-effort (`""` on any
  failure) like `_read_acoustid_tag`.
- **RISK — provenance chain (F1/F3).**  Capturing the hash/fingerprint inside the copy/tag/verify
  loop must not break the AGENTS.md journal-provenance invariant: the `action="tagged"` entry is
  appended only after `_verify_copy` succeeds.  Capture the values pre-tag but journal them at the
  existing append site (`_pipeline.py:1292`), never earlier.

---

## Action-frame digest

Appended by `@plan-admin` on non-trivial iterations (discovery flagged, contract flexed, or
meaningful texture).  Trivial iterations (clean green run, no surprises) produce no entry.  Fed
verbatim into every `@plan-deep` juncture fork.

### F0 — 2026-06-02
Discovery/flex: audio_sha256 → audio_hash rename + algorithm-tagged format (approved at sign-off); uniform decoded-PCM SHA-256 would require a new binary dependency violating rung-3 "Binary: none".
Affected: C-F0a (field name), C-F0c (function name + return format), KAT name
Deferred: no — resolved at sign-off; all downstream sessions updated to consume audio_hash.
Texture: FLAC uses STREAMINFO md5_signature (format "flac-md5:<32hex>"); MP3 uses SHA-256 of raw MPEG frames post-ID3 (format "mp3-stream-sha256:<64hex>"); algorithm prefix prevents silent cross-format collision.

---

## Folded-in / retired items

- **Old `docs/PLAN.md §307` — Chromaprint `--verify-fingerprints` source-check.**  Subsumed by this
  plan: the source-vs-MB fingerprint verification it described *is* rung 5 (F6,
  `fetch_acoustid_lookup`) plus the ingest identity-confirm.  F8 records its retirement from
  `docs/PLAN.md`.  The `--acoustid-key` mechanism the §307 note flagged as "needed" is delivered in
  F6 as a CLI flag.
