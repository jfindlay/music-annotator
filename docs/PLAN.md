<!-- juncture-tier: opus -->
<!-- sub-track: R3a (PrestoMusic download adapter) — ROADMAP critical-path; 2nd J1-ordered R3 adapter; 36 clean dirs -->

# PLAN — R3a: PrestoMusic download adapter (ISRC identity → tier promotion)

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Add **PrestoMusic download source-adapter support** so the 36 census-clean Presto dirs ingest at their best
achievable annotation tier.  Presto downloads are the second J1-ordered R3 adapter (descending clean population,
after R3b's 52 whipper dirs).  Unlike whipper rips, Presto downloads **legitimately carry no provenance sidecar**
(NOTES.md: "legitimately absent for PrestoMusic downloads") — no rip log, no TOC disc-ID, no `00 - disc info.yaml`.
Their one intrinsic identity artifact is that **every track carries an ISRC** (BACKLOG: "PrestoMusic files always
carry ISRCs").

**What is genuinely new (the survey narrowed the roadmap's ~2-session estimate to a contract-sharp 3).**  The
ISRC *identity* machinery already exists and is R3b-proven: `_isrc_matches` (archival identity **rung 1**,
`_pipeline_io.py:331`), `_read_isrc_tag`, `TrackTags.isrc`, the TSRC round-trip in `_tagger.py`, and `"isrc"` in
`_IDENTITY_METHODS`.  What does **not** exist is the wiring from ISRC identity to the **annotation tier**.  The
census-signal ladder in `run()` (`_pipeline.py:1729–1735`) promotes to `full-mb-verified` only on TOC-match or
embedded recording-MBID, and otherwise falls through to `SEARCH_HIT` → `mb-search-resolved` + `needs_spot_check`.
So a Presto dir whose ISRCs match the resolved MB release lands at the *same* tier as a bare text-search hit,
despite carrying a definitive offline identity signal.  R3a closes exactly this gap — the ISRC analogue of R3b's
single-disc-TOC promotion:

1. **ISRC-match tier promotion (the reserved unwired signal).**  Add a `CensusSignal.ISRC_MATCH` that
   `classify_annotation_tier` maps to `full-mb-verified` / `needs_spot_check=False`, and wire `run()`'s signal
   ladder to select it when the source ISRCs match the **selected medium's** recording `isrc_list`s (evidence,
   not mere presence).  Freezes **C-ISRC**.  Additive to C-TIER: extends the open `CensusSignal` enum and the
   classifier's match arms; **does not touch** `AnnotationTier` / `ANNOTATION_TIER_ORDER` / the classifier
   signature (that would be a destructive-HALT — see R-1).
2. **Presto download recognition.**  A recogniser alongside R3b's `is_whipper_dir`: a dir whose audio files
   carry ISRC tags and which bears no competing rip-provenance signature (no whipper log, no TOC) → sets
   `origin_source = "presto"`.  Freezes **C-PRESTO**.  The signature is *ISRC-presence* (recognition); the
   *ISRC-match against the candidate* (S1) is what promotes the tier — the two-step exactly parallels R3b
   (recognise the dir on the artifact; gate the promotion on the evidence).
3. **Spot-check/audit surfacing + integration.**  Presto/ISRC-promoted entries flow through R3b's existing
   spot-check gate and audit tier-pass; extend the audit surfacing so an ISRC-verified entry is distinguished
   from a bare search-resolved one, and add a Presto end-to-end integration test.

**Faithful to the identity semantics, not an over-claim.**  An ISRC identifies a *recording*, not a *release* —
one recording appears on many releases.  So ISRC-*presence* alone cannot claim release-identity; only an ISRC
**match against the selected medium's recordings** (the reconciled release) licenses `full-mb-verified`.  A
Presto dir with ISRCs that do **not** match the resolved release stays `mb-search-resolved` + `needs_spot_check`
— recognition succeeds (`origin_source="presto"`) but promotion does not fire.  This is the ISRC analogue of
R3b gating single-disc promotion on `toc_matched`, not on `origin_source` alone.

## Verify gate

Touches `src/` and `tests/`; fully gated (100% branch coverage, strict mypy).  `/plan-run` re-discovers these;
stated here to document the gate:

- **VERIFY_TEST**: `~/.local/bin/tox -e test` — pytest, **100% branch coverage enforced** (`fail_under = 100`).
  Every new signal-selection branch (ISRC-all-match / ISRC-partial / ISRC-absent / ISRC-mismatch) and every
  recognition branch needs explicit KATs, or coverage fails.  The new `classify_annotation_tier` arm needs its
  own case test.
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` — mypy strict on `src/ tests/`, **zero errors**.  No `Any`,
  no `cast()`.
- Full gate before ◆ close: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format +
  check_lint 10.00/10 + check_upgrade) green.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 @architect | Promote ISRC-match identity to `full-mb-verified` (freeze **C-ISRC**) | A | Opus | C-TIER, `CensusSignal`/`classify_annotation_tier`, `_isrc_matches` (rung 1), `run()` signal ladder | `src/music_annotator/models.py`, `src/music_annotator/_pipeline.py`, `tests/unit/test_models.py`, `tests/unit/test_pipeline.py` |
| 2 | Recognise Presto download dirs and set `origin_source` (freeze **C-PRESTO**) | B | Sonnet | **C-PRESTO** (S1 freeze), C-WHIP (mutual-exclusion with whipper) | `src/music_annotator/_discover.py`, `tests/unit/test_discover.py` |
| 3 ◆ | ISRC-verified audit surfacing + Presto integration test | I | Opus | **C-ISRC** (S1), **C-PRESTO** (S2), C-TIER, all above | `src/music_annotator/_audit.py`, `tests/integration/test_integration.py` |

`Cat`: A = substrate (freezes a contract downstream rows consume) · B = algorithm (self-contained against frozen
substrate) · I = integrative (audit surfacing + end-to-end).  `Tier`: Opus on S1 (the C-ISRC tier-promotion is
correctness-critical — a wrong promotion silently marks a mis-matched release as identity-verified; same
cost-of-error domain as R3b S3) and S3 (the ◆ boundary review + end-to-end verification of the promotion under
the real mutagen/journal path); Sonnet on S2 (mechanical recogniser against a frozen contract).  `@architect` on
S1 — C-ISRC's signal shape + the evidence gate are the one genuine design surface and the inflection point the
juncture fork resolves into Cross-session contracts at execution time.  `◆` on S3 — sub-track-final; its boundary
hands off to the R3e other-download adapter shard (a separate `/plan-shard`), not to an adjudication fork.

**Split/merge rationale (levers named).**  Roadmap estimated ~2 sessions; sharded to **3**.  The +1 is driven by
**lever 3 (cost of design error)** and **lever 4 (correctness-criticality)**: C-ISRC promotes to the strongest
tier on offline evidence, so isolating it in its own Opus substrate session (dense green checkpoint, clean
revert) is worth the extra warm-up (**lever 5**'s excellent inner loop makes it cheap).  Net is still *below*
R3b's 5 because the ISRC identity machinery pre-exists (no C-AR-scale model freeze).  The **one-line-commit-title
corollary** forced the S1/S2 split at a contract-sharp boundary: S1 freezes C-ISRC (the signal + promotion the
recogniser feeds), S2 consumes it — "promote ISRC identity *and* recognise Presto dirs" is two titles.  S2/S3
stay split because S3 also carries the audit-surfacing deliverable and the ◆ review; folding the integration test
into the recogniser session would merge a Sonnet mechanical row with an Opus integrative boundary.

## Session detail

### S1 @architect — Promote ISRC-match identity to `full-mb-verified` (freeze C-ISRC)

**Deliverable.**  Wire ISRC identity into the annotation-tier signal:
- Add **`CensusSignal.ISRC_MATCH = "isrc-match"`** to the `CensusSignal` StrEnum (`models.py`).
- Add the mapping arm to **`classify_annotation_tier`**: `case CensusSignal.ISRC_MATCH: return
  AnnotationTier.FULL_MB_VERIFIED, False`.  Additive — the `AnnotationTier` vocabulary and the function
  signature are untouched.
- In **`run()`'s signal ladder** (`_pipeline.py:1729–1735`), insert an ISRC-match rung **between** the TOC/
  embedded-MBID rung and the `SEARCH_HIT` fallback: when every source track's ISRC matches an ISRC in the
  corresponding selected-medium recording's `isrc_list` (reuse `_isrc_matches` / `_read_isrc_tag`), select
  `CensusSignal.ISRC_MATCH`.  The match is against the **selected medium** (the reconciled release), so a Presto
  dir whose ISRCs don't match the resolved release stays `SEARCH_HIT`.

**Over-specify (Category-A).**  Freeze in C-ISRC the *evidence rule* (all-tracks-match vs any-track-match) and the
`ISRC_MATCH` literal now — changing the threshold later re-touches the classifier and the ladder.  Decide and
pin: **all present-ISRC tracks must match** (a single ISRC mismatch drops to `SEARCH_HIT`); tracks lacking a
source ISRC are permitted (partial-ISRC dirs still promote if no mismatch) — this is the resolvable design lever
the @architect fork settles at freeze time and writes into the C-ISRC subsection.

**≥1 KAT.**  (a) `test_isrc_all_match_yields_full_verified` — single-medium release, all source ISRCs match the
medium recordings' `isrc_list`s, no embedded MBID, no TOC; assert `CensusSignal.ISRC_MATCH` → tier
`full-mb-verified` + `needs_spot_check == False` (was `mb-search-resolved` + `True`).  (b)
`test_isrc_mismatch_stays_search_resolved` — one source ISRC not in the candidate list → `SEARCH_HIT` (no
promotion).  (c) `test_classify_isrc_match_arm` — the new `classify_annotation_tier` case maps `ISRC_MATCH` →
`(FULL_MB_VERIFIED, False)`.  Retain the existing embedded-MBID and search-hit tests unchanged (no regression).

**Subtleties.**
- **Do not weaken C-TIER.**  If the change seems to need a *new tier* (e.g. an intermediate "isrc-verified" rung
  between search-resolved and full-verified), **HALT** — that is the destructive-HALT signal that C-TIER was
  mis-frozen (R-1).  The resolved design is a new *signal* mapping to an *existing* tier, nothing more.
- **`match/case` exhaustiveness.**  The new arm sits before the existing `case _: # pragma: no cover` in
  `classify_annotation_tier`; keep the wildcard.
- **Evidence, not recognition.**  S1 must not depend on `origin_source == "presto"` (that is S2's recogniser).
  The promotion is evidence-gated on the ISRC match itself, so it fires for *any* ISRC-matching dir, not only
  recognised-Presto ones — the recogniser (S2) is for provenance labelling, not for gating the tier.  This is a
  deliberate divergence from R3b S3 (which gated on `origin_source=="whipper"`): a TOC disc-ID from an untrusted
  source is weak, but an ISRC *match against the resolved release* is self-validating regardless of source.

**Deferrals.**  Per-track ISRC persistence to the sidecar/journal for later audit is S3's concern.  Any Picard
tag-key alignment for ISRC is R6c (ISRC already round-trips as TSRC/`isrc` — no new key).

### S2 — Recognise Presto download dirs and set `origin_source` (freeze C-PRESTO)

**Deliverable.**  A recogniser in `_discover.py` alongside `is_whipper_dir` (S-R3b): a source dir is a **Presto
download** when its audio files carry ISRC tags **and** it bears no competing strong rip-provenance signature (no
whipper log via `_find_whipper_log`, no `00 - disc info.yaml`, no TOC).  On recognition, `discover()` sets
`origin_source = "presto"` (following the existing whipper wiring at `_discover.py:1020–1024`, idempotent-write
per the `ProvenanceSidecar` rule).  Whipper recognition takes precedence (mutual exclusion — a whipper rip that
happens to carry ISRCs is whipper, not Presto).

**≥1 KAT.**  `test_presto_dir_recognised` — a dir with ISRC-bearing FLACs and no rip-provenance artifact →
`origin_source == "presto"`.  `test_whipper_precedence_over_presto` — a dir with both a whipper log and ISRCs →
`origin_source == "whipper"` (mutual exclusion).  `test_no_isrc_not_presto` — a dir with no ISRC tags is not
recognised as Presto (`origin_source == ""`).

**Subtleties.**  Reading ISRC presence reuses `_read_isrc_tag` (best-effort, returns `""` on unreadable) — a dir
where *no* file yields an ISRC is not Presto; define C-PRESTO's threshold (any-file vs all-files ISRC presence)
and pin it.  The recogniser is *recognition only* — it must not itself promote the tier (that is S1's
evidence-gated ladder); `origin_source` is a provenance label.

**Deferrals.**  **R3e overlap is real and deliberate (R-2):** other-download/amazon dirs (R3e, 19 dirs) may also
carry ISRCs and would be recognised by this same signature.  C-PRESTO names the *recognition* as ISRC-presence;
whether R3e collapses into this mechanism (a source-variant label) or gets its own `origin_source` is an R3e
shard decision, not R3a's.  Do not widen R3a to handle R3e dirs — additive-reshard if the census surfaces the
need.  Presto-specific artifact detection (booklet/receipt filenames) is deferred: no confirmed Presto artifact
across the 36 dirs, and ISRC-presence is the reliable intrinsic signal.

### S3 ◆ — ISRC-verified audit surfacing + Presto integration test

**Deliverable.**  (a) Extend the R3b `_audit_tier_pass` (`_audit.py:283`) so an entry promoted via
`ISRC_MATCH` is **distinguished** from a bare `mb-search-resolved` entry and from a TOC/AR-verified one — surface
the identity basis (ISRC) in the `audit_tier_*` log event, so an operator sees *why* a release is full-verified.
(b) A Presto **integration test** in `tests/integration/test_integration.py` exercising the full public path on
an embedded Presto-shaped fixture (ISRC-bearing FLAC bytes, no rip log, no disc info): dir recognition
(`origin_source == "presto"`) → MB search resolution (mocked) → ISRC-match tier promotion to `full-mb-verified` →
tags written and read back through the real mutagen path → journal + confirmation message correct.

**≥1 KAT.**  The integration test is the primary KAT (end-to-end, no internal-helper patching per the integration
convention).  Plus `test_audit_distinguishes_isrc_verified` (audit surfaces an ISRC-promoted entry distinctly
from a search-resolved one).

**Subtleties.**  The audit change is *incremental* on R3b's existing tier-pass — do not restructure the pass; add
the ISRC distinction to the existing enumeration.  The integration fixture must carry a **matching** ISRC (source
ISRC ∈ the mocked recording's `isrc_list`) so the promotion actually fires — a fixture with a non-matching ISRC
tests only the search-resolved fallback and would silently fail to exercise S1's promotion.  The Presto ingest
rides the confirmation-provenance invariant unchanged (Presto has no sidecar to preserve — no new sidecar path).

**Deferrals.**  Bulk operator drain of the 36 dirs is R5 (operator-paced).  R3e adapter shard is the ◆ handoff.

## Cross-session contracts

### C-ISRC — ISRC-match tier promotion *(FROZEN at S1)*

The rule by which an ISRC match against the reconciled MB release promotes the annotation tier to
`full-mb-verified`.  **Flavour: compiler-enforced** (the `CensusSignal.ISRC_MATCH` enum value + the
`classify_annotation_tier` mapping arm) + **test-enforced** (KATs pin the ladder selection and the tier result).

**Additive to C-TIER, not a re-freeze.**  C-ISRC adds a `CensusSignal` value and a classifier match arm mapping
it to the **existing** `AnnotationTier.FULL_MB_VERIFIED`.  It does **not** alter `AnnotationTier`,
`ANNOTATION_TIER_ORDER`, `annotation_tier_rank`, or the `classify_annotation_tier` *signature* — all frozen at
R2 (C-TIER).  The `CensusSignal` enum and the classifier's match arms are C-TIER's designed growth points (R2
left `alternate-source` signal-less by intent).

**The `ISRC_MATCH` literal.**  `CensusSignal.ISRC_MATCH = "isrc-match"` — the exact string `"isrc-match"`
(lowercase, hyphenated).  Added as a member of the `CensusSignal` StrEnum in `models.py`.

**The classifier arm.**  `case CensusSignal.ISRC_MATCH: return AnnotationTier.FULL_MB_VERIFIED, False` — added to
`classify_annotation_tier` **immediately before** the `case _: # pragma: no cover` wildcard (which is retained).
No signature change, no tier-vocabulary change, no order-map change (parallels the existing
`case CensusSignal.EMBEDDED_MBID` arm exactly).

**The ladder placement.**  The rung is inserted in `run()`'s signal ladder (`_pipeline.py`, the `else` branch at
~1731–1735) **after** the `has_embedded_mbid` check and **before** the `SEARCH_HIT` fallback assignment.  The
resolved ladder order is therefore: TOC / embedded-MBID → **ISRC-match** → `SEARCH_HIT`.  The rung is evaluated
only when neither TOC nor embedded-MBID already fired.

**The match target.**  The **selected medium's recordings** — each source track's embedded ISRC is checked
against its corresponding `selected_medium.track_list[*].recording.isrc_list` (the reconciled release), **never**
a raw search candidate.  This is why the promotion licenses *release*-identity, not mere recording-identity
(R-3): a dir whose ISRCs match some MB recording but not the recordings on the reconciled selected medium does
**not** promote.  Per-track correspondence is positional — `src_files[i]` maps to the selected medium's `i`-th
track (`copy_subset`), and `src_files`/`copy_subset` are guaranteed equal-length by the `RuntimeError` track-count
guard that precedes the ladder (`_pipeline.py:1553`).

**The evidence rule (frozen — all edge cases enumerated).**  Over the selected medium's tracks paired
positionally with the source files, evaluate each source track's embedded ISRC against its corresponding
recording's `isrc_list` via `_isrc_matches` (archival identity rung 1, `_pipeline_io.py:331`; its tri-state
`.match` is `True` = source ISRC ∈ `isrc_list`, `False` = source ISRC present but not in a non-empty `isrc_list`,
`None` = source ISRC unreadable/absent **or** candidate `isrc_list` empty).  Promote to `CensusSignal.ISRC_MATCH`
iff **both**:
- **(a) no mismatch** — no track yields `.match == False`; a single `False` drops the whole dir to `SEARCH_HIT`;
  **and**
- **(b) at least one confirmed match** — at least one track yields `.match == True`.

Edge cases:
- a track with no source ISRC, or whose corresponding recording has an empty `isrc_list`, yields `.match == None`
  (inconclusive) — it neither blocks promotion (does not violate (a)) nor counts toward (b); a **partial-ISRC**
  dir still promotes provided ≥1 track matches and none mismatch;
- a dir where **no** track produces `.match == True` (all `None`) does **not** promote — it stays `SEARCH_HIT`.
  This subsumes both the "no source ISRCs at all" case and the `isrc_list`-unpopulated caveat below.

**Implementation note (not a contract — conservative fallthrough).**  `MBRecording.isrc_list` populates only when
the `"isrcs"` include is passed to the recording fetch (models.py:960); an unpopulated list is `[]`, which
`_isrc_matches` treats as inconclusive (`None`).  By rule (b) this keeps the tier at `SEARCH_HIT` rather than
over-promoting — a safe fallthrough.  S1's implementer must ensure ISRCs are available on the compared recordings
(or accept the conservative fallthrough); this is an availability concern for the implementer, not a widening of
C-ISRC.

**Reuses the identity rung, does not alter it.**  The rung reads `_isrc_matches`'s per-track ISRC verdict to
drive the orthogonal **annotation-tier** axis; it does **not** modify the archival **identity-confidence** rung
ladder (`_IDENTITY_METHODS`, rung 1).  Never conflate the two ladders.

- **Defined-in:** S1 (`models.py`: `CensusSignal.ISRC_MATCH` + classifier arm; `_pipeline.py`: the ladder rung).
  **Consumed-by:** S3 (audit surfacing reads the ISRC identity basis; integration test asserts the promotion).
- **KATs that pin C-ISRC (S1):** `test_isrc_all_match_yields_full_verified`,
  `test_isrc_mismatch_stays_search_resolved`, `test_classify_isrc_match_arm` (named above).  Coverage
  (`fail_under = 100`, R-5) additionally requires branch KATs for the **partial-ISRC-no-mismatch** promotion path
  and the **all-inconclusive** (no confirmed match → `SEARCH_HIT`) path.

### C-PRESTO — PrestoMusic download recognition *(to be frozen at S2)*

What makes a source dir a PrestoMusic download.  **Flavour: prose-enforced** (recognition heuristic) +
**test-enforced** (KAT per branch).  Implemented S2 in `_discover.py` alongside `is_whipper_dir`.

**Recognition heuristic.**  A source dir is a **Presto download** when **both**:
1. its audio files carry **ISRC tags** (threshold pinned at S2 — proposed: **any** audio file yields a
   non-empty ISRC via `_read_isrc_tag`); **and**
2. it bears **no competing strong rip-provenance signature** — no whipper native log (`_find_whipper_log` →
   `None`), no `00 - disc info.yaml`, no resolvable TOC.

**`origin_source` value:** the literal string **`"presto"`** (lowercase, exact), written once per the
`ProvenanceSidecar` idempotent-write rule.  **Whipper precedence:** whipper recognition runs first; a dir
matching both is whipper.

**Recognition-vs-evidence separation.**  C-PRESTO governs the *provenance label* (`origin_source`); the *tier
promotion* is C-ISRC's evidence-gated ladder (S1), which fires on the ISRC **match** independent of
`origin_source`.  A recognised-Presto dir whose ISRCs don't match the resolved release is still `origin_source ==
"presto"` but stays `mb-search-resolved`.

**Signature is broader than Presto (R3e note).**  ISRC-presence recognises any ISRC-bearing download, not Presto
uniquely.  C-PRESTO names the recognition; R3e decides whether other-download/amazon dirs share the `"presto"`
label or get their own.  Do not widen R3a for R3e.

- **Defined-in:** S2.  **Consumed-by:** S3 (integration test asserts `origin_source == "presto"`; audit may
  surface it).
- **KATs that pin C-PRESTO (S2):** `test_presto_dir_recognised`, `test_whipper_precedence_over_presto`,
  `test_no_isrc_not_presto` (named above).

### Consumed (frozen upstream — invalidation is a destructive-HALT)

- **C-TIER** (R2 S1): `AnnotationTier` + `ANNOTATION_TIER_ORDER` + `annotation_tier_rank` +
  `classify_annotation_tier` *signature* + `ProvenanceSidecar.annotation_tier`/`needs_spot_check` +
  monotonic-upgrade carve-out.  R3a **consumes** it: S1 adds a `CensusSignal` + a mapping arm (the designed
  growth point) but **must not** edit the tier vocabulary, the order map, or the classifier signature.  If R3a
  appears to need a new *tier*, HALT.  **Flavour: compiler+test-enforced.**
- **C-WHIP** (R3b S1): the whipper recognition contract.  S2 consumes it for mutual exclusion (whipper
  precedence) — a Presto recogniser must call the same whipper-detection helpers, not re-implement them.
  **Flavour: prose+test-enforced.**
- **Archival identity rung ladder** (`_IDENTITY_METHODS`, `_isrc_matches` rung 1, `_pipeline_io.py`): S1 reuses
  the existing ISRC-match machinery; it does **not** alter the rung ladder (identity-confidence axis), only wires
  its ISRC verdict into the orthogonal annotation-tier axis.  Never conflate the two ladders (the two-ladder
  note, ROADMAP/NOTES).
- **C-PROV / C-MOVE + confirmation-provenance invariant** (repo `AGENTS.md`): unchanged — Presto has no
  provenance sidecar to preserve, so R3a adds no new copy/verify path; the "safe to delete source" message stays
  derived only from verified `action == "tagged"` entries.  **Flavour: prose+test-enforced.**

### Produced

- **C-ISRC** (S1) and **C-PRESTO** (S2) — see above.  No other new contract.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Promote ISRC-match identity to full-mb-verified | pending | — | C-ISRC |
| 2 | Recognise Presto download dirs and set origin_source | pending | — | C-PRESTO |
| 3 | ISRC-verified audit surfacing + Presto integration test | pending | — | — |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **R-1 (C-TIER is frozen at the tier vocabulary; the signal enum is the seam — destructive-HALT boundary).**
  R3a's promotion enters by adding `CensusSignal.ISRC_MATCH` + a classifier arm to the **existing**
  `full-mb-verified` tier.  If an executor finds themselves editing `AnnotationTier`, `ANNOTATION_TIER_ORDER`,
  `annotation_tier_rank`, or the `classify_annotation_tier` signature — or reaching for a *new* intermediate tier
  — that is scope drift into a frozen contract: **destructive-HALT**.  The resolved design is signal-additive
  only.
- **R-2 (C-PRESTO's ISRC-presence signature is broader than Presto — additive-reshard signal for R3e).**  Any
  ISRC-bearing download matches the recogniser; R3e (other-download/amazon, 19 dirs) will likely overlap.  This
  is expected, not a defect.  If R3e dirs need distinct provenance handling, that is an R3e shard decision
  (additive-reshard), not an R3a widening.  Do not fold R3e dirs into R3a.
- **R-3 (ISRC is recording-identity, not release-identity — the match-target guard is load-bearing).**  The
  promotion gates on ISRC match against the **selected medium's** recordings.  If an executor relaxes this to
  ISRC-*presence* or match against a raw search candidate, the tier will over-claim release-identity for dirs on
  the wrong pressing.  Keep the match target the reconciled release; this guard is C-ISRC, not incidental.
- **R-4 (spot-check interaction — the promotion removes `needs_spot_check`).**  ISRC-promoted Presto dirs become
  `full-mb-verified` + `needs_spot_check=False`, so they **exit** the S5-R3b spot-check population.  This is
  intended (an ISRC match is stronger evidence than a bare search hit).  Watch item: if the ISRC-match evidence
  rule turns out to admit wrong-pressing matches (same recording, different release), that would be a
  false-promotion — surface at the ◆ as an additive-reshard (tighten the evidence rule) or destructive-HALT (if
  it implies ISRC-match cannot license `full-mb-verified` at all).
- **R-5 (coverage on the ISRC-evidence branches).**  `fail_under = 100`: each of all-match / partial-ISRC-no-
  mismatch / any-mismatch / no-source-ISRC, plus the new classifier arm, needs a KAT.  A green `check_type` with
  red coverage is the expected failure mode if a branch KAT is forgotten — a checklist item, not a surprise.

## Notes for executors

- **Tier routing.**  S1/S3 are Opus (`@architect`/juncture-tier: opus); S2 is Sonnet (`@build`).  S1 is the
  `@architect` inflection row — the juncture fork resolves C-ISRC's `ISRC_MATCH` literal + evidence rule +
  match-target into the Cross-session contracts section at execution time and writes them there before S2/S3
  consume them.  ROADMAP `juncture-tier: opus` stands and this sub-track keeps it (lever 4: the tier-promotion is
  correctness-critical — a false promotion silently marks a mis-matched release identity-verified; lever 5's
  strong inner loop alone does not license opting down while lever 4 is high).
- **Register: PEDAGOGY off** — thin mechanical docstrings per house style (Sphinx/PEP 257, 128-col).  Design
  rationale lives in this PLAN; a one-line comment at the ISRC-ladder rung noting the recording-vs-release
  identity distinction suffices.
- **Invariants to preserve (do not regress):** C-TIER's tier vocabulary + classifier signature (untouched — S1
  adds a signal + arm only); the two-ladder separation (annotation tier ≠ identity-confidence rung — S1 wires
  the ISRC *rung verdict* into the *tier* axis, never conflating them); the confirmation-provenance chain
  (unchanged — no new sidecar path); the `ProvenanceSidecar` idempotent-write rule on `origin_source`.
- **No `Any`, no `cast()`.**  `match/case` with `case _: # pragma: no cover` retained on `classify_annotation_tier`.
- **Full gate before ◆ / each commit:** `~/.local/bin/tox -m analyze` green (100% branch cov, mypy strict, pylint
  10.00/10, pyupgrade clean).
- **Sequencing:** R3a is the **2nd** J1-ordered R3 adapter, deriving from R3b's proven adapter shape (recognise-
  then-gate-promotion two-step; `is_whipper_dir` → `is_presto_dir` parallel).  On the S3 ◆, R3a hands off to the
  R3e other-download adapter shard (a separate `/plan-shard` — 19 dirs; may reuse C-PRESTO's recogniser).
- **Suggested `/plan-run` invocation:** `/plan-run halt-at-junctures` — R3b proved the adapter shard pattern, so
  R3a runs junctures-only (halt at S1's @architect inflection to resolve C-ISRC, then run through to the ◆);
  the pattern no longer needs a full boundary halt.
