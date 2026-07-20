<!-- juncture-tier: opus -->
<!-- sub-track: R2 (annotation-tier substrate) — ROADMAP critical-path; after J1, before R3 adapters -->

# PLAN — R2: annotation-tier substrate

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Build the **annotation-tier substrate**: the present-state authority that records, for every ingested
release, *how completely it could be annotated* — so that coverage-before-quality is honored (every dir
ingested at its best achievable tier, provisionality persisted as a first-class fact) and upgrades are
enumerable (Act III-b re-resolves below-full entries as better data appears).  R2 freezes **C-TIER**, the
annotation-tier contract every R3 adapter consumes, and adds an `audit` pass that enumerates provisional
entries.  This is the Category-A substrate on the critical path: **J1 → R2 → R3 (binding adapter) → R5**.

**Annotation tier ≠ identity rung (the two-ladder distinction — do not conflate).**  The codebase already
uses "rung" for an **archival-identity-confidence** ladder (rung 0 embedded tags → rung 1 ISRC → … → rung
5 keyed AcoustID, in `_pipeline_io.py`/`__main__.py`): *how confidently a file matches an MB recording*.
R2's **annotation tier** is the orthogonal axis: *how completely a release could be annotated*.  A dir can
be high-tier / low-rung (full MB annotation, identity only from source tags) or low-tier / high-rung
(source-tags-only ingest, but a strong AcoustID identity).  **Keep "rung" for identity; use "tier" (never
"rung") for annotation completeness throughout R2.**  Reusing "rung" would collide with load-bearing
existing code.

**Substrate over-specification (Category-A discipline).**  C-TIER carries all five tiers — including the
`alternate-source` tier that has **zero census population today** (R3c Discogs pruned by J1).  Carrying the
empty tier now is deliberate: adding it later would re-freeze the contract every adapter consumes.  Same
for the `needs-spot-check` flag on `mb-search-resolved` — it exists to make the search-only-confidence
concern (J1) persisted and `audit`-discoverable, even though the spot-check itself lands in R3.

## Verify gate

R2 touches `src/` and `tests/` and is **fully gated** (unlike R0, which lived in `scripts/` outside the
gates — this is a KAT-enforced substrate, not a deliverable-checked artifact).  `/plan-run` re-discovers
these; stated here to document the gate:

- **VERIFY_TEST**: `~/.local/bin/tox -e test` — pytest, **100% branch coverage enforced** (`fail_under =
  100`).  Every new tier value, every audit-pass branch, every match/case arm needs an explicit test.
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` — mypy strict on `src/ tests/`, **zero errors**.  No
  `Any`, no `cast()`.
- Full gate before any ◆ close: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format +
  check_lint 10.00/10 + check_upgrade) must be green.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 | Freeze annotation-tier vocabulary + persist `ANNOTATION_TIER` on the sidecar | A | Sonnet | C-PROV/C-MOVE, ProvenanceSidecar (models.py), PROVENANCE_FILENAME (`_pipeline_io.py`) | `src/music_annotator/models.py`, `src/music_annotator/_pipeline_io.py`, `tests/unit/test_models.py`, `tests/unit/test_pipeline.py` |
| 2 | `audit` pass: enumerate provisional (below-full) entries + upgrade candidates | A | Sonnet | **C-TIER** (S1), `_audit.py` counter/pass structure, journal action vocabulary | `src/music_annotator/_audit.py`, `src/music_annotator/__init__.py`, `tests/unit/test_annotator.py` |
| 3 ◆ | Wire tier assignment into the ingest/tag write path (default `mb-search-resolved`/`full`) | A | Sonnet | **C-TIER** (S1), `_tags.py`/`_tagger.py` write path, `_pipeline.py` provenance-append ordering | `src/music_annotator/_tags.py`, `src/music_annotator/_pipeline.py`, `tests/unit/test_pipeline.py`, `tests/integration/test_integration.py` |

`Cat`: A = substrate (all three — R2 is one Category-A unit split at contract-sharp boundaries: S1 freezes
the interface S2/S3 consume).  `Tier`: Sonnet throughout — no session carries an open design surface once
C-TIER is frozen at S1 (the one design decision, the tier vocabulary, is J1 output + this derivation, not
an executor's call).  `◆` = sub-track-final row; **no juncture fires at R2's ◆** — R2 hands off to the R3
adapter shards (each its own `/plan-shard`), not to an adjudication juncture.  No `@architect` inflection
row: the substrate shape is fixed by J1 + this PLAN, so no executor faces an interface-design decision.

## Session detail

### S1 — freeze annotation-tier vocabulary + persist `ANNOTATION_TIER`

**Deliverable.**  The C-TIER contract frozen in code: (a) the five tier identifiers as a closed vocabulary
(a `StrEnum` or `Literal` union — an enum is preferred for match/case exhaustiveness and mypy), (b) an
`annotation_tier: str = ""` field (defaulting to unset) plus a `needs_spot_check: bool = False` field on
`ProvenanceSidecar` (models.py), (c) read/write support in `_read_provenance_sidecar` /
`_write_provenance_sidecar` preserving the existing idempotent "written once, other keys preserved"
invariant, (d) a helper that maps a census-style classification to a tier (pure, unit-testable).

Tier vocabulary (frozen — R3 adapters and `audit` consume these exact strings):

| Tier | Meaning | Entry criterion | `needs_spot_check` |
|------|---------|-----------------|--------------------|
| `full-mb-verified` | identity-confirmed full MB annotation | embedded MBID **or** TOC disc-ID identity match; track count reconciles | `false` |
| `mb-search-resolved` | search-reconciled MB annotation, *lower confidence* | in-mb-clean via MB **search** (track-count reconciliation, not identity) | **`true`** until spot-checked |
| `mb-partial` | declared track/structure mismatch tolerated | MB release identified but track/structure disagrees; mismatch recorded | `false` |
| `alternate-source` | non-MB external identity (Discogs-style) — **reserved, empty today** | identity from external source; no MB | `false` |
| `source-tags-only` | no MB identity; provisional minimal | ingest from embedded/source tags only | `false` |

**≥1 KAT.**  `test_annotation_tier_vocabulary_roundtrips` — write each tier + `needs_spot_check` to a sidecar,
read back, assert equality; `test_tier_classifier_maps_census_signals` — the classification→tier helper maps
each census axis-2 signal (embedded-MBID → `full-mb-verified`, search-hit → `mb-search-resolved`, mismatch →
`mb-partial`, not-in-mb → `source-tags-only`) to the correct tier.  (C-TIER's deliverable *is* a KAT — the
contract is behavioural.)

**Subtleties.**
- **Idempotency invariant** (existing, `ProvenanceSidecar`): fields written once, never overwritten; other
  keys preserved.  Adding `annotation_tier` must not break this — but a *tier upgrade* (Act III-b) is a
  legitimate overwrite.  Resolve now: `annotation_tier` is overwritable **only monotonically upward** (a
  re-resolve may raise the tier, never silently lower it); record the design in the field docstring.  This
  is a prose sub-contract of C-TIER.
- **Lossless principle**: an unset/empty `annotation_tier` on an ingested entry is a *defect*, not a valid
  state — the whole point is that provisionality is persisted, never silent.  S3 makes the write path always
  set it; S2's audit flags any empty one.
- **No `Any` / no `cast()`**: the tier enum + `populate_by_name` model config keep this clean.

### S2 — `audit` pass: enumerate provisional entries + upgrade candidates

**Deliverable.**  A new `audit` pass (following the existing `_make_audit_counts` / `_audit_*` multi-pass
structure in `_audit.py`) that reads `annotation_tier` from each entry's sidecar and enumerates: count per
tier, the below-`full-mb-verified` (provisional) population, and the `needs_spot_check` population.  New
counter keys in `_AUDIT_COUNT_KEYS` (e.g. `tier_full`, `tier_search`, `tier_partial`, `tier_alt`,
`tier_source_only`, `provisional_total`, `needs_spot_check`).  Logs one event per finding, consistent with
the existing audit event vocabulary.  This is the "`audit` enumerates provisional entries cheaply / upgrade
candidates discoverable" ROADMAP requirement.

**≥1 KAT.**  `test_audit_enumerates_tiers` — a fixture library with a mix of tiers; assert the per-tier
counts and the provisional total; `test_audit_flags_needs_spot_check` — assert the search-resolved
population is surfaced.

**Subtleties.**
- **Journal action vocabulary** (repeated R0 hazard, now `src/`-side): `_audit_journal_scan` filters
  `action in {"tagged", "enriched"}`.  The tier pass keys off the *sidecar*, not the journal action — but
  confirm the eligible-entry set matches so tier counts and existing audit counts reconcile against the same
  denominator.
- **Sidecar-per-work-dir vs entry-per-file**: `ProvenanceSidecar` is per work_top_dir; audit counts are per
  destination.  State the aggregation explicitly (a work_dir's tier applies to all its tracks) and test the
  multi-track case.

### S3 ◆ — wire tier assignment into the ingest/tag write path

**Deliverable.**  The ingest path (`_pipeline.py` / `_tags.py`) sets `annotation_tier` on the provenance
sidecar at ingest time, derived from the identity evidence available (embedded MBID/TOC → `full-mb-verified`;
search hit → `mb-search-resolved` + `needs_spot_check=true`; mismatch → `mb-partial`; no MB →
`source-tags-only`).  The default clean-ingest path assigns `full-mb-verified` or `mb-search-resolved` per
the evidence.  End-to-end integration test proves the real write-and-read-back path (per the integration-test
convention: no internal helpers patched).

**≥1 KAT.**  Integration test `test_ingest_persists_annotation_tier` — run the pipeline on a fixture release
with an embedded MBID, assert the sidecar carries `full-mb-verified`; a second fixture resolved by search
asserts `mb-search-resolved` + `needs_spot_check`.

**Subtleties.**
- **Confirmation-provenance invariant (FROZEN — repo AGENTS.md).**  The tier write must slot into the
  copy/tag/verify/journal-append ordering **without disturbing it**: the `action="copied"` journal entry and
  the "Verified OK" message still derive exclusively from post-verification in-memory state.  Write the tier
  to the sidecar *within* the already-verified region (after `_verify_copy` succeeds), never before.  A tier
  write that appends before verification, or that becomes a new source for the confirmation message, violates
  the invariant → destructive-HALT.
- **Layer-routing rule** (NOTES): tier assignment is a policy/provenance concern — keep it in the
  provenance-sidecar layer, not smeared into the MB-data or renderer layers.

**Deferrals.**  The **spot-check gate** on the `mb-search-resolved` population lands in the **first R3
adapter** (R3b), not R2 — R2 only persists the `needs_spot_check` flag that makes it discoverable.  Tier
*upgrades* (re-resolve below-full entries) are Act III-b.  The `alternate-source` tier stays adapter-less
until R3c is un-pruned.

## Cross-session contracts

### Consumed (frozen upstream — invalidation is a destructive-HALT)

- **C-PROV / C-MOVE** (move/verify/journal provenance, NOTES + repo AGENTS.md) — the tier write is a new
  sidecar field *inside* the existing verified region; it must not alter the provenance chain.
- **Confirmation-provenance invariant** (repo AGENTS.md) — S3 writes the tier only after `_verify_copy`
  succeeds; the "safe to delete source" message's evidence basis is unchanged.
- **`ProvenanceSidecar` idempotency** (existing, models.py / `_pipeline_io.py`) — written-once, other-keys-
  preserved; `annotation_tier` extends it with a *monotonic-upgrade* carve-out (S1 subtlety).
- **The identity-rung ladder** (`_pipeline_io.py` `_IDENTITY_METHODS`, rungs 0–5) — R2 must **not** rename,
  reuse, or collide with "rung"; annotation tier is a distinct axis (Purpose two-ladder note).
- **Prose contracts** (NOTES): lossless principle (unset tier = defect, not silent state); "journal detects,
  tag adjudicates" (tier is present-state authority on the sidecar; journal is the detector); layer-routing.

### Produced

- **C-TIER** (frozen at S1; consumed by S2, S3, and every R3 adapter): the five-value annotation-tier
  vocabulary (`full-mb-verified` / `mb-search-resolved` / `mb-partial` / `alternate-source` /
  `source-tags-only`), the `ANNOTATION_TIER` + `needs_spot_check` persistence on `ProvenanceSidecar`, the
  classification→tier mapping, and the monotonic-upgrade rule.  **Flavour: compiler-enforced** (the tier enum
  is a closed type mypy checks at every consumer) **+ test-enforced** (KATs on round-trip and classification)
  **+ prose** (monotonic-upgrade rule, unset=defect).  Over-specified: carries the empty `alternate-source`
  tier and the `needs_spot_check` flag whose consumer (spot-check gate) is in R3.  Stability horizon:
  runtime contract for all of R3 and Act III-b.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Freeze annotation-tier vocabulary + persist `ANNOTATION_TIER` | done | d679394 | C-TIER (AnnotationTier StrEnum, annotation_tier + needs_spot_check on ProvenanceSidecar, classify_annotation_tier helper, monotonic-upgrade rule) |
| 2 | `audit` pass: enumerate provisional + upgrade candidates | done | 8ad5c70 | — (extra: tests/unit/test_audit.py — regression fix for existing audit tests after new tier pass added) |
| 3 ◆ | Wire tier assignment into the ingest/tag write path | done | dab0343 | — (note: _tags.py not modified — tier assigned in _pipeline.py per layer-routing rule; fix-loop iter 1 for ruff format + pylint unused-import/reimport) |

## Action-frame digest

### S3 — 2026-07-20
Discovery/flex: _tags.py not modified — tier assignment implemented entirely in _pipeline.py, consistent with layer-routing rule (tier is provenance/policy, not tag-rendering).
Affected: none (expected-files prediction was conservative; implementation is correct)
Deferred: no
Texture: Fix-loop iteration 1 consumed for ruff format + pylint (unused import + reimport in test_pipeline.py); gate green on second pass.

## Discoveries & risks

- **R-1 (two-ladder collision — resolved in derivation, watch in execution).**  "rung" is taken by the
  identity-confidence ladder; annotation completeness is "tier".  If an executor reaches for "rung" for tier,
  or the two ladders get wired into one field, HALT and surface — this is the contract's central naming
  invariant.  (internal-continue if caught early; the collision itself is already adjudicated.)
- **R-2 (idempotency vs upgrade tension).**  `ProvenanceSidecar` is written-once; annotation-tier needs a
  monotonic-upgrade carve-out (Act III-b re-resolves upward).  S1 must state this precisely; a naive "never
  overwrite" breaks Act III-b, a naive "always overwrite" breaks the idempotency invariant.  If S1 cannot
  reconcile cleanly, surface at ◆ (additive-reshard: the upgrade semantics may want their own small session).
- **R-3 (confirmation-provenance is a destructive-risk surface).**  S3 touches the copy/tag/verify loop.  A
  tier write placed before `_verify_copy` succeeds, or feeding the confirmation message, is a
  **destructive-HALT** — the invariant is frozen (repo AGENTS.md).  Never ride through.
- **R-4 (empty `alternate-source` tier).**  Carrying a tier with zero population and no adapter is deliberate
  over-specification, not dead code to prune.  If an executor proposes removing it "because it's unused,"
  that's a contract regression — refuse and cite Category-A over-specification.
- **R-5 (search-confidence is real, not paranoia).**  The `needs_spot_check` flag exists because R0
  adjudication caught score-100-but-wrong MB matches.  If S3's classifier marks search hits as
  `full-mb-verified` (dropping the distinction), the whole point of the tier is lost — the search/identity
  boundary is load-bearing.

## Notes for executors

- **Tier routing.**  S1/S2/S3 are all Sonnet (`@build`).  No juncture fires inside R2; the ◆ boundary hands
  off to the R3 adapter shards (each a separate `/plan-shard`), not to an adjudication fork.  ROADMAP
  `juncture-tier: opus` stands but is not exercised here.
- **Register: PEDAGOGY off** — thin mechanical docstrings per house style (Sphinx/PEP 257, 128-col); the
  design exposition lives in this PLAN and the ROADMAP, not inline.
- **Invariants to preserve (do not regress):** confirmation-provenance ordering (S3), `ProvenanceSidecar`
  idempotency + monotonic-upgrade (S1), the identity-rung / annotation-tier axis separation (all), lossless
  principle (unset tier = defect).  All are C-TIER's consumed or produced contracts above.
- **Full gate before each ◆ / commit:** `~/.local/bin/tox -m analyze` green (100% branch cov, mypy strict,
  pylint 10.00/10, pyupgrade clean).  R2 is `src/`-side — the R0 "outside the gates" posture does **not**
  apply.
- **Sequencing:** the **pre-R3 `_parse_release_item` fix** (ROADMAP; own session) and **PLAN R1-F** both
  sequence before any R3 adapter but are **independent of R2** — R2 can proceed in parallel with or before
  them; only R3 depends on all three.
- **Suggested `/plan-run` invocation:** `/plan-run halt-at-boundaries` — R2 is a fresh shard pattern
  (first `src/`-side substrate sharded under this tuning law); halting at the ◆ hands the C-TIER-frozen
  substrate to the user for review before the R3 adapter shards derive from it.
