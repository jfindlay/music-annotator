<!-- juncture-tier: opus -->
<!-- sub-track: R3b (whipper/MakeMKV rip source adapter) — ROADMAP critical-path; first J1-ordered R3 adapter; 52 clean dirs -->

# PLAN — R3b: whipper source adapter (TOC identity + AccurateRip provenance)

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Add first-class **whipper-rip source-adapter support** so the 52 census-clean whipper dirs ingest at their
best achievable annotation tier with their rip-fidelity provenance preserved.  Whipper is the highest-identity-
confidence provenance in the corpus: every whipper rip carries a **MusicBrainz TOC disc-ID** (hardware-level
identity) and an **AccurateRip** verification result (bit-fidelity against a crowd consensus of the same
pressing).  This is J1's first-ordered R3 adapter (descending clean population → maximises R5 drain-unlock).

**What is genuinely new (the survey narrowed the roadmap's ~3-session estimate to a sharp 4).**  The
TOC→MB→tier machinery mostly *already exists*: `parse_disc_toc`, `_toc_lookup_mb_releases`, `_match_medium_by_toc`,
and `CensusSignal.EMBEDDED_MBID → full-mb-verified` (frozen at C-TIER/R2) are all in place.  R3b adds the four
seams that are not:

1. **AccurateRip provenance (the reserved 4th archival dimension).**  `TrackTags`/`TransactionEntry` carry a
   reserved comment slot `# --- archival identity (extensible: 4th dim slots in here) ---` (models.py:1335,
   1643) next to the `audio_hash`/`chromaprint_fp`/`acoustid_id` triple.  AccurateRip is per-track (two DB
   generations v1/v2, each Result+Confidence+CRC), so **per-track data lands in the tags** (the reserved slot;
   round-trips through mutagen; verified by `_verify_copy`), and the **per-release summary** (MB disc-ID, CDDB
   disc-ID, log SHA-256, accurate/in-DB counts) lands in the **sidecar** (`ProvenanceSidecar`).  Freezes **C-AR**.
2. **Whipper dir recognition.**  Nothing today promotes a whipper signature to a provenance signal —
   `parse_dir_hint` *strips* the `.0x…` freedb-CRC suffix as noise.  R3b adds recognition (whipper `.log`
   present, `00 - disc info.yaml` present, `.0x…` suffix) → sets `origin_source` and the whipper-log parse path.
   Freezes **C-WHIP**.
3. **TOC-disc-ID → `full-mb-verified` regardless of medium count.**  `run()` currently promotes to
   `EMBEDDED_MBID` only when `toc_matched` on a *multi-disc* selection (_pipeline.py:1571 "multi-disc only").
   A single-disc whipper rip whose FLACs lack embedded MBIDs falls through to `SEARCH_HIT` (→ needs-spot-check)
   despite a resolving TOC disc-ID.  R3b makes TOC-disc-ID identity yield `full-mb-verified` whatever the medium
   count, with whipper as the trust anchor.  Consumes C-TIER; does not alter it.
4. **The J1-mandated spot-check gate.**  Before the first direct-ingest adapter bulk-runs, spot-check a sample
   of the `mb-search-resolved` population; `needs_spot_check` is the persisted, `audit`-discoverable mechanism.
   R3b folds this gate in (S4) as J1 prescribed.

**Faithful to the standard, not a nonstandard invention.**  C-AR mirrors whipper's own `WhipperLogger` schema
1:1 (v1/v2 per track; Result ∈ {exact-match, no-exact-match, not-present}; Confidence:int; Local/Remote CRC).
Whipper writes a well-formed YAML log with a self-attesting SHA-256; we preserve that log byte-exact as a
sidecar *and* materialise its structured content — lossless capture of the AccurateRip convention.

## Verify gate

Touches `src/` and `tests/`; fully gated (100% branch coverage, strict mypy).  `/plan-run` re-discovers these;
stated here to document the gate:

- **VERIFY_TEST**: `~/.local/bin/tox -e test` — pytest, **100% branch coverage enforced** (`fail_under = 100`).
  Every new AccurateRip parse branch (exact-match / no-match / not-present / v1-only / v2-only / absent) and the
  whipper-recognition branches need explicit KATs, or coverage fails.
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` — mypy strict on `src/ tests/`, **zero errors**.  No `Any`
  (use the `JSON` alias for opaque whipper-log YAML only until parsed into the C-AR models), no `cast()`.
- Full gate before ◆ close: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format +
  check_lint 10.00/10 + check_upgrade) green.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 @architect | Add AccurateRip provenance models + tag/sidecar round-trip (freeze **C-AR**, **C-WHIP**) | A | Opus | C-TIER, C-PROV/C-MOVE, `TrackTags`/`TransactionEntry` 4th-dim slot, `ProvenanceSidecar`, whipper `WhipperLogger` schema | `src/music_annotator/models.py`, `src/music_annotator/_tagger.py`, `tests/unit/test_models.py`, `tests/unit/test_pipeline.py` |
| 2 | Parse whipper log into C-AR models (`parse_whipper_log`) | B | Sonnet | **C-AR** (S1), **C-WHIP** (S1) | `src/music_annotator/_pipeline_io.py`, `tests/unit/test_pipeline.py` |
| 3 | Promote TOC-disc-ID identity to `full-mb-verified` regardless of medium count | A | Opus | C-TIER, **C-WHIP** (S1) | `src/music_annotator/_pipeline.py`, `tests/unit/test_pipeline.py` |
| 4 | Wire whipper adapter into discovery + preserve `.log`/`.cue` sidecars; recognise whipper dir | B | Sonnet | **C-WHIP** (S1), **C-AR** (S1), C-MOVE | `src/music_annotator/_discover.py`, `src/music_annotator/_pipeline.py`, `tests/unit/test_discover.py` |
| 5 ◆ | Spot-check gate on `mb-search-resolved` population + `audit` surfacing + whipper integration test | I | Opus | **C-AR**, **C-WHIP**, C-TIER, all above | `src/music_annotator/_audit.py`, `src/music_annotator/_pipeline.py`, `tests/integration/test_integration.py` |

`Cat`: A = substrate (freezes a contract downstream rows consume) · B = algorithm (self-contained against frozen
substrate) · I = integrative (spot-check gate + end-to-end).  `Tier`: Opus on S1 (contract-shape design surface,
high cost-of-error), S3 (TOC-identity/provenance-critical logic in the confirmation-provenance domain), and S5
(the J1 spot-check gate is a policy decision on the whole search-resolved population); Sonnet on S2/S4 (mechanical
parse + wiring against frozen contracts).  `@architect` on S1 — the C-AR substrate interface is the one genuine
design surface and is the inflection point the juncture fork resolves at execution time.  `◆` on S5 — sub-track-
final; its boundary hands off to the R3a Presto adapter shard (a separate `/plan-shard`), not to an adjudication
fork.

**Split/merge rationale (levers named).**  Roadmap estimated ~3 sessions; sharded to 4 implementation + 1
integrative = **5**.  The upward adjustment is driven by **lever 3 (cost of design error)** and **lever 4
(correctness-criticality)**: C-AR is a compiler-enforced contract on the provenance/confirmation chain, so
denser green checkpoints and finer reverts are worth the extra warm-ups (lever 5's excellent inner loop makes
them cheap).  The **one-line-commit-title corollary** forced two splits that a 3-session cut would have merged:
(a) S1 (freeze C-AR) and S2 (parse the log into C-AR) split at a contract-sharp boundary — S1 freezes the model
the S2 parser targets; merging them is "add the models *and* the parser," two titles.  (b) S3 (tier promotion)
and S4 (adapter wiring) split because S3 is a `_pipeline.py` tier-logic change and S4 is `_discover.py` +
sidecar-preservation wiring — "promote TOC identity" and "wire the whipper adapter" are two commit titles.  S5
is separated per the integrative-session pacing rule (consistently under-scheduled) *and* because the J1
spot-check gate is a distinct deliverable, not a tail of the wiring session.

## Session detail

### S1 @architect — AccurateRip provenance models + tag/sidecar round-trip (freeze C-AR, C-WHIP)

**Deliverable.**  Freeze the AccurateRip provenance contract (**C-AR**) mirroring whipper's `WhipperLogger`
schema, split by cardinality per the persistence decision (per-track → tags, per-release → sidecar):
- **Per-track (tags).**  A structured model — sketch: `AccurateRipTrackResult{version: str, result: <enum
  exact-match | no-exact-match | not-present>, confidence: int = 0, local_crc: str = "", remote_crc: str = ""}`
  — carried for both `v1` and `v2`, plus `test_crc`/`copy_crc`/`status`.  Add an `accuraterip` field to
  **`TrackTags`** and **`TransactionEntry`** in the reserved `# --- 4th dim slots in here ---` slot
  (models.py:1335, 1643).  Wire the round-trip in `_tagger.py` (`_MP3_TXXX_MAP` + FLAC key mapping) so it
  survives mutagen write-and-read-back and `_verify_copy`.
- **Per-release (sidecar).**  Add `accuraterip_summary` to **`ProvenanceSidecar`** — sketch:
  `AccurateRipSummary{mb_disc_id: str, cddb_disc_id: str, log_sha256: str, accurately_ripped: int,
  in_ar_database: int, summary_text: str}`.  Subject to the monotonic-upgrade rule already on the sidecar (a
  re-resolve may enrich, never silently drop a present summary).
- Freeze **C-WHIP**: the whipper source signature (what makes a dir a whipper rip) as a named prose+test
  contract, so S2/S4 consume a stable definition.

**Over-specify (Category-A).**  Carry the fields whipper emits even if S2–S5 don't consume all of them yet:
`test_crc`, `copy_crc`, per-track `status`, `local_crc`/`remote_crc`.  Adding them later re-freezes C-AR (a
tag-schema migration through the whole confirmation-provenance chain) — far costlier than carrying them now.
This is the reserved-4th-dimension slot the BACKLOG explicitly designed for ("the 4th field appends without
renaming or restructuring").

**≥1 KAT.**  (a) `test_accuraterip_track_tag_roundtrip` — a `TrackTags` with a populated v1 exact-match + v2
no-match `accuraterip`; apply to a FLAC and an MP3, read back, assert equality (the real mutagen path, not a
mock).  (b) `test_accuraterip_summary_monotonic` — a present summary is not dropped by a later empty-summary
write.  (c) `test_accuraterip_result_enum_exhaustive` — the three Result states round-trip.

**Subtleties.**
- **No `Any`.**  Whipper-log YAML is opaque only until parsed; the C-AR models are fully typed.  The `JSON`
  alias may appear at the parse boundary (S2), never in the C-AR models themselves.
- **`match/case` exhaustiveness.**  The Result enum needs a `case _: # pragma: no cover` arm per house style.
- **Tag key naming — Picard alignment is deferred.**  Choose whipper-faithful TXXX/FLAC keys now
  (e.g. `ACCURATERIP_V1_CONFIDENCE`); the R6c Picard-alignment session owns any later renaming.  Note the
  choice in C-AR so R6c can find it.
- **Do not touch the tier logic here.**  S1 is model + round-trip only; the `full-mb-verified` promotion is S3.

**Deferrals.**  MakeMKV (video-disc rips) shares the "rip source" umbrella but emits no AccurateRip; C-WHIP
names whipper only, and MakeMKV recognition is deferred (no census population pressures it now — the 52 dirs are
whipper).  AccurateRip *backfill* into already-ingested library files is Act III-b (`audit --enrich` per BACKLOG
P-FP3), not R3b.

### S2 — Parse whipper log into C-AR models (`parse_whipper_log`)

**Deliverable.**  A `parse_whipper_log(src_dir) -> (AccurateRipSummary, dict[int, per-track AR])` in
`_pipeline_io.py`, parallel to the existing `parse_disc_toc`.  Reads the whipper native-logger YAML (well-formed
`ruamel`/YAML per `WhipperLogger`): the `CD metadata` block (MB disc-ID, CDDB disc-ID), the per-track `Tracks`
`AccurateRip v1`/`v2` blocks, the `Conclusive status report` summary, and the trailing `SHA-256 hash` line.
Verify the log's self-attesting SHA-256 against the recomputed hash of the log body; a mismatch is a warning
(the log was edited), not a hard failure (the audio may still be fine).

**≥1 KAT.**  `test_parse_whipper_log_full` against an embedded minimal whipper-log fixture (all-accurate case);
`test_parse_whipper_log_no_ar_database` (none-in-DB case → summary text + zero counts);
`test_parse_whipper_log_partial_match` (some tracks no-match).  Fixture is a trimmed real-shape log string
constant, per the test-substrate convention for FLAC/MP3 bytes.

**Subtleties.**  Empty/absent AccurateRip block per track (`Track not present in AccurateRip database`) → the
`not-present` enum, not an error.  HTOA track 0 handling (whipper emits a track `0`) — map or skip explicitly.
The log SHA-256 is over the log *body* before the hash line is appended (see `WhipperLogger.logRip`).

**Deferrals.**  EAC-logger-plugin variant logs (whipper supports an `eac` logger) — out of scope; C-WHIP names
the native logger.  If a whipper dir uses the eac logger, additive-reshard.

### S3 — Promote TOC-disc-ID identity to `full-mb-verified` regardless of medium count

**Deliverable.**  In `run()` (_pipeline.py ~1566–1584), lift the `toc_matched → EMBEDDED_MBID` promotion out of
its implicit multi-disc restriction so a resolving TOC disc-ID yields `CensusSignal.EMBEDDED_MBID`
(→ `full-mb-verified`, `needs_spot_check=False`) on single-disc rips too.  Whipper provenance (`origin_source`
set in S4, or the presence of a validated whipper log) is the trust anchor that licenses this — a TOC disc-ID
from an untrusted source is weaker, so gate the single-disc promotion on whipper provenance being present.

**≥1 KAT.**  `test_single_disc_toc_yields_full_verified` — single-medium release, TOC disc-ID matches, whipper
provenance present, source FLACs carry NO embedded MBID; assert tier `full-mb-verified` + `needs_spot_check ==
False` (was `mb-search-resolved` + `True`).  Retain the existing multi-disc TOC test and the no-TOC search-hit
test — the change must not regress them.

**Subtleties.**  **Do not weaken C-TIER.**  The tier vocabulary and `classify_annotation_tier` signature are
frozen (R2); this session changes only which `CensusSignal` `run()` selects, upstream of the classifier.  If the
change seems to need editing `AnnotationTier` or `classify_annotation_tier`, HALT — that is a destructive-HALT
signal that C-TIER was mis-frozen.  The whipper-provenance gate keeps a bare non-whipper single-disc TOC match
at its current (conservative) tier — the promotion is *whipper-anchored*, not a blanket loosening.

**Deferrals.**  Non-whipper TOC sources (e.g. a hand-placed `00 - disc info.yaml`) keep current behaviour; any
future generalisation is its own session.

### S4 — Wire whipper adapter into discovery + preserve `.log`/`.cue` sidecars

**Deliverable.**  (a) Whipper-dir **recognition** in `_discover.py` (new helper alongside `parse_dir_hint`):
whipper `.log` present and/or `00 - disc info.yaml` present and/or `.0x…` freedb suffix → set
`origin_source = "whipper"` and route the AccurateRip parse (S2) + TOC lookup (existing).  (b) **Sidecar
preservation** in `_pipeline.py`, parallel to `_write_freedb_yaml`: copy `whipper.log` / `.cue` / `.toc` into
the work dir with a SHA-256 integrity check and a `"sidecar"` journal entry (C-MOVE provenance chain).  (c)
Populate `TransactionEntry.accuraterip` (per-track) and `ProvenanceSidecar.accuraterip_summary` (per-release)
from S2's parse, threaded through `_copy_tag_verify_journal_pass`.

**≥1 KAT.**  `test_whipper_dir_recognised` (each signature independently → `origin_source == "whipper"`);
`test_whipper_log_preserved_with_integrity` (log copied, SHA-256 matches, journal `"sidecar"` entry present);
`test_accuraterip_threaded_to_journal` (per-track AR reaches the `TransactionEntry`).

**Subtleties.**  `_EXCLUDED_FILENAMES` (_pipeline_io.py:57) — ensure whipper non-audio sidecars aren't picked up
as tracks (they have non-audio extensions, so the extension filter already excludes them; confirm).  The
sidecar copy must ride the **confirmation-provenance invariant**: a `"sidecar"` journal entry is not a
`"copied"`/`"tagged"` entry and must not feed the "safe to delete source" message (that message derives only
from verified `action == "tagged"` entries — do not regress this).

**Deferrals.**  Cover-art / booklet handling for whipper dirs reuses the existing path; no new work.

### S5 ◆ — Spot-check gate on `mb-search-resolved` + audit surfacing + integration test

**Deliverable.**  (a) The **J1 spot-check gate**: before whipper dirs bulk-ingest, surface the sample of the
`mb-search-resolved` (`needs_spot_check == True`) population for human confirmation, using the persisted
`needs_spot_check` flag; make it `audit`-discoverable (extend the `_audit.py` tier-enumeration pass from R2 S2
to enumerate the spot-check population with its AccurateRip status attached, so a rip that is AccurateRip-verified
but only search-resolved is visibly distinguished).  (b) A whipper **integration test** in
`tests/integration/test_integration.py` exercising the full public path on an embedded whipper-shaped fixture
(FLAC bytes + minimal whipper log): dir recognition → TOC lookup (mocked MB) → tier promotion → AccurateRip tags
written and read back through the real mutagen path → sidecar preserved → journal + confirmation message correct.

**≥1 KAT.**  The integration test is the primary KAT (end-to-end, no internal-helper patching per the integration
convention).  Plus `test_audit_enumerates_spot_check_population` (audit surfaces `needs_spot_check` entries with
AR status).

**Subtleties.**  The spot-check gate is a *surfacing/enumeration* mechanism, not an automatic re-tiering — human
confirmation is out-of-band (operator election), and confirming clears `needs_spot_check` monotonically (never
re-raises it).  A high false-match rate here is the J1 watch item that could reshard R3 order — if the spot-check
sample shows many score-100-but-wrong matches, that is an **additive-reshard** signal (tighten the search-resolve
scoring), possibly a destructive-HALT if it invalidates C-TIER's `mb-search-resolved` entry criterion.

**Deferrals.**  Bulk operator drain of the 52 dirs is R5 (operator-paced, no agent session).  The spot-check
*policy* (sample size, acceptance threshold) is an operator decision surfaced here, not hardcoded.

## Cross-session contracts

### C-AR — AccurateRip provenance *(FROZEN at S1 — resolved by @architect juncture fork)*

The per-track + per-release AccurateRip provenance contract, mirroring whipper's `WhipperLogger` schema
(`whipper/result/logger.py`, read 1:1 at freeze time).  **Flavour: compiler-enforced** (Pydantic models +
tag-key mapping) at the per-track/`TrackTags`/`TransactionEntry` and per-release/`ProvenanceSidecar` surfaces;
**test-enforced** at the mutagen round-trip (KAT pins tag survival).

#### Load-bearing resolution — the flat-`str` round-trip constraint (read before implementing)

The tag round-trip layer is a **flat `dict[str, str]`**, not a nested-object channel.  `_read_tags_flac`
returns `{KEY.upper(): first_value}`; `_read_tags_mp3` inverts `_MP3_TXXX_MAP`; `_verify_copy` asserts
`_read_tags_*(dest) == tags.to_file_dict()`; `_tags_from_file_dict` (enrich/repath) maps each lowercase key back
to a `TrackTags` field.  **Consequence:** the per-track AccurateRip data carried *on the tags* cannot be a nested
Pydantic model — it must be **flat scalar `str` fields** on `TrackTags`/`TransactionEntry` (exactly like the
existing `audio_hash`/`chromaprint_fp`/`acoustid_id` triple in the same 4th-dim slot).  The **structured nested
models** (`AccurateRipResult`, `AccurateRipTrackResult`, `AccurateRipTrack`) exist for the S2 parse product and
typed manipulation; S4 **projects** their content onto the flat tag fields (write) and reconstructs where needed.
This is not a departure from the PLAN sketch — it *is* "per-track data lands in the tags, round-trips through
mutagen, verified by `_verify_copy`" made concrete against the actual round-trip machinery.

#### 1. Structured models (`models.py`) — S2 parse product + typed surface

```python
class AccurateRipResult(StrEnum):
    """Per-version AccurateRip verification outcome (whipper WhipperLogger.trackLog `Result`)."""
    EXACT_MATCH   = "exact-match"    # whipper "Found, exact match"
    NO_EXACT_MATCH = "no-exact-match" # whipper "Found, NO exact match"
    NOT_PRESENT   = "not-present"    # whipper "Track not present in AccurateRip database"
    # match/case consumers add `case _: # pragma: no cover` per house style.

class AccurateRipTrackResult(BaseModel):
    """One AccurateRip DB generation (v1 or v2) for one track."""
    version: str = ""                        # "v1" | "v2"
    result: AccurateRipResult = AccurateRipResult.NOT_PRESENT
    confidence: int = 0                      # whipper DBConfidence; 0 when not-present
    local_crc: str = ""                      # whipper "Local CRC", uppercase hex ("" when not-present)
    remote_crc: str = ""                     # whipper "Remote CRC", uppercase hex ("" when not-present)

class AccurateRipTrack(BaseModel):
    """Per-track AccurateRip container: both DB generations + rip CRCs + status (S2 parse product)."""
    v1: AccurateRipTrackResult = Field(default_factory=AccurateRipTrackResult)
    v2: AccurateRipTrackResult = Field(default_factory=AccurateRipTrackResult)
    test_crc: str = ""                       # whipper "Test CRC" (%08X), uppercase hex
    copy_crc: str = ""                       # whipper "Copy CRC" (%08X), uppercase hex
    status: str = ""                         # whipper "Status": "Copy OK" | "Error, CRC mismatch" |
                                             #   "Track not ripped (skipped)"

class AccurateRipSummary(BaseModel):
    """Per-release AccurateRip summary (whipper "CD metadata" + "Conclusive status report")."""
    mb_disc_id: str = ""                     # whipper "MusicBrainz Disc ID"
    cddb_disc_id: str = ""                   # whipper "CDDB Disc ID"
    log_sha256: str = ""                     # trailing "SHA-256 hash:" line (uppercase hex)
    accurately_ripped: int = 0               # whipper _accuratelyRipped counter
    in_ar_database: int = 0                  # whipper _inARDatabase counter
    summary_text: str = ""                   # whipper "AccurateRip summary" message line
```
**Over-specified (Category-A):** `test_crc`, `copy_crc`, `status`, `local_crc`, `remote_crc` are carried now even
though S2–S5 don't all consume them — adding a field later re-freezes C-AR (a tag-schema migration through the
confirmation-provenance chain).  The `JSON` alias appears only at the S2 YAML-parse boundary, never in these
models.

#### 2. Flat per-track fields on `TrackTags` and `TransactionEntry` (the tag round-trip surface)

Landing **inside the existing `# --- archival identity (extensible: 4th dim slots in here) ---` slot**
(models.py:1335 on `TrackTags`, models.py:1643 on `TransactionEntry`), appended after
`acoustid_id`/`chromaprint_fp` — the append-without-restructure guarantee.  All fields are `str` (default `""`),
so `to_file_dict()` emits them only when non-empty and `_read_tags_*` round-trips them unchanged.  Field names
are the lowercased tag keys (so `_tags_from_file_dict` reconstructs them as named fields, not extras):

| `TrackTags`/`TransactionEntry` field | FLAC Vorbis key / MP3 TXXX key (identical) | MP3 TXXX desc | serialized as |
|---|---|---|---|
| `accuraterip_v1_result`      | `ACCURATERIP_V1_RESULT`      | `ACCURATERIP_V1_RESULT`      | `str` (enum value: `exact-match`/`no-exact-match`/`not-present`) |
| `accuraterip_v1_confidence`  | `ACCURATERIP_V1_CONFIDENCE`  | `ACCURATERIP_V1_CONFIDENCE`  | `str` (decimal int; `""` when 0/absent) |
| `accuraterip_v1_local_crc`   | `ACCURATERIP_V1_LOCAL_CRC`   | `ACCURATERIP_V1_LOCAL_CRC`   | `str` (uppercase hex) |
| `accuraterip_v1_remote_crc`  | `ACCURATERIP_V1_REMOTE_CRC`  | `ACCURATERIP_V1_REMOTE_CRC`  | `str` (uppercase hex) |
| `accuraterip_v2_result`      | `ACCURATERIP_V2_RESULT`      | `ACCURATERIP_V2_RESULT`      | `str` (enum value) |
| `accuraterip_v2_confidence`  | `ACCURATERIP_V2_CONFIDENCE`  | `ACCURATERIP_V2_CONFIDENCE`  | `str` (decimal int; `""` when 0/absent) |
| `accuraterip_v2_local_crc`   | `ACCURATERIP_V2_LOCAL_CRC`   | `ACCURATERIP_V2_LOCAL_CRC`   | `str` (uppercase hex) |
| `accuraterip_v2_remote_crc`  | `ACCURATERIP_V2_REMOTE_CRC`  | `ACCURATERIP_V2_REMOTE_CRC`  | `str` (uppercase hex) |
| `accuraterip_test_crc`       | `ACCURATERIP_TEST_CRC`       | `ACCURATERIP_TEST_CRC`       | `str` (uppercase hex) |
| `accuraterip_copy_crc`       | `ACCURATERIP_COPY_CRC`       | `ACCURATERIP_COPY_CRC`       | `str` (uppercase hex) |
| `accuraterip_status`         | `ACCURATERIP_STATUS`         | `ACCURATERIP_STATUS`         | `str` (whipper Status text verbatim) |

**All tag values are `str`.** `confidence` is an `int` in `AccurateRipTrackResult` but serializes to a **decimal
string** in the tag layer (empty string when 0/absent, so `to_file_dict()`'s non-empty filter drops it and the
round-trip stays exact — do not emit `"0"`).  Wiring: add each key to `_MP3_TXXX_MAP` with desc == key (the
project's own-namespace convention, as with `AUDIO_HASH`/`CHROMAPRINT_FP`); FLAC needs no table (it writes every
`to_file_dict()` key lowercased).  A convenience projection helper (`AccurateRipTrack.to_tag_fields() ->
dict[str, str]` and inverse) may live on the model or in `_tagger.py` — implementation choice, not contract; the
contract is the field names, keys, and `str`-serialization above.

**Tag-key naming is whipper-faithful, Picard alignment deferred (R5-R6c owns any rename).**  These keys are an
own-namespace choice; R6c may map/alias them to Picard conventions later.  Recorded here so R6c can find them.

#### 3. Per-release summary on `ProvenanceSidecar` (YAML sidecar, not tag round-trip)

Add `accuraterip_summary: AccurateRipSummary = Field(default_factory=AccurateRipSummary)` to `ProvenanceSidecar`.
Serialized in the `freedb_disc_N.yaml` / `music_annotator_provenance.yaml` sidecar (YAML — nested model is fine
here; the flat-`str` constraint is a *tag-layer* constraint only).

**Monotonic-upgrade rule** (extends the existing `ProvenanceSidecar` carve-out): a re-resolve may **enrich** the
summary but must **never silently drop a present one**.  Concretely, when merging a new summary into an existing
sidecar, treat a present `log_sha256` (or any non-empty summary) as authoritative: an incoming **empty**
`AccurateRipSummary` must not overwrite a populated one (the S1 KAT `test_accuraterip_summary_monotonic` pins
this).  Field-level enrichment (filling a previously-empty field) is permitted; wholesale replacement of a
populated summary with an empty one is forbidden.  This mirrors the `annotation_tier` upward-only rule already on
the model.

#### Surfaces & pins

- **Defined-in:** S1 (`models.py`: the four models + flat fields + sidecar field; `_tagger.py`: `_MP3_TXXX_MAP`
  additions).  **Consumed-by:** S2 (parse target — produces `AccurateRipTrack` + `AccurateRipSummary`), S4
  (projects per-track onto `TransactionEntry` flat fields; sets `accuraterip_summary`), S5 (audit surfacing reads
  summary + per-track status).
- **KATs that pin C-AR (S1):** (a) `test_accuraterip_track_tag_roundtrip` — a `TrackTags` with populated v1
  exact-match + v2 no-match flat fields; `apply_tags_flac` **and** `apply_tags_mp3`, read back via the real
  mutagen path (not a mock), assert `_read_tags_*(dest) == tags.to_file_dict()` including the AR keys.  (b)
  `test_accuraterip_summary_monotonic` — a present `accuraterip_summary` survives a later empty-summary merge.
  (c) `test_accuraterip_result_enum_exhaustive` — the three `AccurateRipResult` values round-trip (value ↔ enum).
  Coverage (`fail_under = 100`) additionally requires the per-branch KATs named in PLAN R-4 (each Result state,
  v1/v2 present/absent combinations) — those land with the S2 parser, not S1.

### C-WHIP — whipper source signature *(FROZEN at S1 — resolved by @architect juncture fork)*

What makes a source dir a whipper rip.  **Flavour: prose-enforced** (recognition heuristic) + **test-enforced**
(KAT per signature).  Recognition is implemented in S4 (`_discover.py`, a helper alongside `parse_dir_hint`);
S1 freezes the definition so S2/S3/S4 consume a stable contract.

#### 1. Recognition heuristic

A source directory is a **whipper rip** when **either** of the two strong signatures is present (`any`, not
`all` — real dirs may lack one):

1. **Whipper native log present** — a `*.log` file whose body ends with a trailing `SHA-256 hash: <UPPERHEX>`
   line (the self-attesting hash `WhipperLogger.logRip` appends).  The `.log` extension **plus** the trailing
   SHA-256 line together constitute the strong whipper-log signature; the SHA-256 line distinguishes a whipper
   native log from an arbitrary `.log` (e.g. an EAC or foreign-ripper log).  *(S2 verifies this hash against the
   recomputed body hash; a mismatch is a warning, not a rejection — the dir is still whipper.)*
2. **`00 - disc info.yaml` present** — the existing `_DISC_INFO_FILENAME` (`_pipeline_io.py:54`), already known to
   the codebase and already in `_EXCLUDED_FILENAMES`.

A **weak corroborating** signal (never sufficient alone — `parse_dir_hint` already strips it as search noise via
`_FREEDB_HEX_SUFFIX_RE`, `_discover.py:46`):

3. **`.0x…` freedb-CRC dir-name suffix** — e.g. `Album Name.0xe212b212`.  Corroborates but does not by itself
   establish a whipper rip (other freedb-era rippers use it too); it raises confidence when combined with (1) or
   (2) but a bare suffix with neither strong signature is **not** recognized as whipper.

**Rationale for the `SHA-256`-line qualifier on signature (1):** S2 defers the EAC-logger-plugin variant
(whipper's `eac` logger) out of scope and additive-reshards if encountered.  The native-logger trailing-hash line
is the cheapest robust discriminator between the native log (in scope) and any other `.log` in the tree, and it
reuses the same artifact S2 already parses — no new probe.

#### 2. `origin_source` value

On recognition, the discovery path sets `ProvenanceSidecar.origin_source = "whipper"` (and
`TransactionEntry`-adjacent provenance follows the existing `origin_time`/`origin_source` idempotent-write rule:
written once, never overwritten).  The literal string is **`"whipper"`** (lowercase, exact).  This is the trust
anchor S3 gates its single-disc TOC→`full-mb-verified` promotion on: the promotion fires only when
`origin_source == "whipper"` (or an equivalently-validated whipper log is present), never on a bare non-whipper
single-disc TOC match.

#### 3. KATs that pin C-WHIP (implemented S4, named here)

- `test_whipper_dir_recognised` — **each strong signature independently** yields recognition → `origin_source ==
  "whipper"`: (a) a dir with only a whipper `.log` (trailing SHA-256 line present); (b) a dir with only
  `00 - disc info.yaml`.  Plus a negative: (c) a dir with **only** the `.0x…` suffix and neither strong signature
  is **not** recognized (weak-signal-alone rejection); (d) a plain `.log` **without** the trailing SHA-256 line is
  not recognized as a whipper native log.
- `test_whipper_log_preserved_with_integrity` (S4) — the recognized log is copied with a SHA-256 integrity check
  and a `"sidecar"` journal entry, riding the C-MOVE chain (and **not** feeding the "safe to delete source"
  message, which derives only from verified `action == "tagged"` entries).

#### Surfaces & pins

- **Defined-in:** S1 (this freeze), S4 (recogniser + sidecar preservation implemented).  **Consumed-by:** S2
  (parse gating — only parse the whipper log when the dir is recognized), S3 (single-disc-promotion trust anchor:
  `origin_source == "whipper"`), S4 (discovery routing).
- **Deferrals (from PLAN, restated so consumers don't widen scope):** MakeMKV is **not** whipper — C-WHIP names
  whipper only; MakeMKV recognition is deferred (no census population).  The whipper `eac` logger variant is out
  of scope — signature (1) is the *native* logger; an `eac`-logger dir is an additive-reshard signal, not a
  silent widening.

### Consumed (frozen upstream — invalidation is a destructive-HALT)

- **C-TIER** (R2 S1): `AnnotationTier` + `classify_annotation_tier` + `ProvenanceSidecar.annotation_tier`/
  `needs_spot_check` + monotonic-upgrade carve-out.  R3b **consumes** it: S3 selects a stronger `CensusSignal`
  upstream of the classifier; S5 enumerates its `needs_spot_check` population.  If R3b appears to need editing
  the tier vocabulary or classifier signature, HALT.  **Flavour: compiler+test-enforced.**
- **C-PROV / C-MOVE** (move/verify/journal provenance) + the **confirmation-provenance invariant** (repo
  `AGENTS.md`): the sidecar-copy path (S4) and AccurateRip-tag write (S1/S4) ride the existing copy→tag→verify→
  journal chain; the "safe to delete source" message stays derived only from verified `action == "tagged"`
  entries.  **Flavour: prose+test-enforced.**
- **`TrackTags` / `TransactionEntry` 4th-dim slot** (models.py:1335, 1643): the reserved comment slot the
  per-track AccurateRip field lands in — the append-without-restructure guarantee the BACKLOG designed.

### Produced

- **C-AR** (S1) and **C-WHIP** (S1) — see above.  No other new contract; S2–S5 consume S1's freeze.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Add AccurateRip provenance models + tag/sidecar round-trip | done | 92d9f7c | C-AR, C-WHIP |
| 2 | Parse whipper log into C-AR models | done | 71d52eb | — |
| 3 | Promote TOC-disc-ID identity to full-mb-verified | pending | — | — |
| 4 | Wire whipper adapter into discovery + preserve sidecars | pending | — | — |
| 5 | Spot-check gate + audit surfacing + whipper integration test | pending | — | — |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **R-1 (C-AR shape is grounded in whipper's real schema — low design risk).**  The `WhipperLogger` format was
  read directly (v1/v2 per-track Result+Confidence+CRC; release-level MB/CDDB disc-ID + log SHA-256).  C-AR
  mirrors it 1:1.  internal-continue while the field shapes match the logger; surface only if a real whipper log
  in the corpus carries fields the model lacks (additive-reshard the field into C-AR).
- **R-2 (single-disc tier promotion must stay whipper-anchored — destructive-HALT boundary).**  S3 loosens the
  TOC→`full-mb-verified` promotion to single-disc, but *only* under whipper provenance.  A blanket loosening
  (any single-disc TOC match → full-verified regardless of source) would weaken C-TIER's confidence semantics —
  if an executor generalises it, that is scope drift; keep it whipper-gated.  If the change reaches into
  `classify_annotation_tier` itself, destructive-HALT.
- **R-3 (spot-check false-match rate is the J1 watch item — additive/destructive signal).**  If S5's spot-check
  sample of the search-resolved population shows many score-100-but-wrong matches, R3 order may need resharding
  (tighten search scoring first).  A rate high enough to invalidate C-TIER's `mb-search-resolved` entry
  criterion is a destructive-HALT; a tolerable rate that just wants a scoring tweak is additive-reshard.
- **R-4 (coverage on the AccurateRip parse branches).**  `fail_under = 100`: every Result state (exact-match /
  no-exact-match / not-present), each of v1-present/v2-present/both/neither, and the log-SHA-256 match/mismatch
  branch need KATs.  A green `check_type` with red `test`-coverage is the expected failure mode if a branch KAT
  is forgotten — a checklist item, not a surprise.
- **R-5 (Picard tag-key alignment is deferred to R6c — do not pre-solve).**  S1 picks whipper-faithful tag keys;
  R6c owns Picard alignment.  Renaming AccurateRip tag keys in R3b to "match Picard" is out-of-scope defocus.
- **R-6 (MakeMKV is named but not built).**  The roadmap says "whipper/MakeMKV"; the census population is
  entirely whipper (52 dirs) and MakeMKV emits no AccurateRip.  C-WHIP names whipper only.  MakeMKV recognition
  is deferred until a census surfaces MakeMKV dirs — not a gap, a scoped decision.

## Notes for executors

- **Tier routing.**  S1/S3/S5 are Opus (`@architect`/juncture-tier: opus); S2/S4 are Sonnet (`@build`).  S1 is
  the `@architect` inflection row — the juncture fork resolves C-AR's field shapes into the Cross-session
  contracts section at execution time and writes them there before S2 consumes them.  ROADMAP `juncture-tier:
  opus` stands and this sub-track keeps it (lever 4: the confirmation-provenance domain is correctness-critical;
  lever 5's strong inner loop alone does not license opting down while lever 4 is high).
- **Register: PEDAGOGY off** — thin mechanical docstrings per house style (Sphinx/PEP 257, 128-col).  Design
  rationale lives in this PLAN; a one-line comment noting the whipper-log AccurateRip shape suffices at the parse
  site.
- **Invariants to preserve (do not regress):** the confirmation-provenance chain (sidecar/AR-tag writes ride
  copy→tag→verify→journal; "safe to delete" derives only from verified `tagged` entries); C-TIER's classifier
  signature and vocabulary (untouched — S3 changes only `run()`'s signal selection); the monotonic-upgrade
  carve-out on `ProvenanceSidecar` (extended to `accuraterip_summary`); the `TrackTags`/`TransactionEntry` 4th-dim
  append-without-restructure guarantee.
- **No `Any`, no `cast()`** — the `JSON` alias only at the whipper-log YAML parse boundary (S2), never in the
  C-AR models.  `match/case` with `case _: # pragma: no cover` on the Result enum.
- **Full gate before ◆ / each commit:** `~/.local/bin/tox -m analyze` green (100% branch cov, mypy strict, pylint
  10.00/10, pyupgrade clean).
- **Sequencing:** R3b is the **first** J1-ordered R3 adapter.  On the S5 ◆, R3b hands off to the R3a Presto
  adapter shard (a separate `/plan-shard` — 36 ISRC-bearing dirs, next by descending clean population).
- **Suggested `/plan-run` invocation:** `/plan-run halt-at-boundaries` — an unproven shard pattern (first R3
  adapter); halt at the S5 ◆ so the C-AR-and-whipper-adapter substrate is reviewed before R3a derives the Presto
  adapter from the now-proven adapter shape.  (After R3b proves the pattern, R3a/R3e may run `halt-at-junctures`.)
