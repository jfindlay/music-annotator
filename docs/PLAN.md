<!-- juncture-tier: sonnet -->
<!-- sub-track: pre-R3 (Category-A hardening) — ROADMAP critical-path; after R1-F, gates all R3 adapters -->

# PLAN — pre-R3: `_parse_release_item` track-count fix

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Repair the **search-result track-count derivation** in `_discover._parse_release_item` so that the count
feeding C-TIER's `mb-search-resolved` classification is correct.  This is the sole remaining prerequisite
gating every R3 adapter (R1-F, the other prerequisite, is done — commit `e7370b7`).

The bug: MB **search** responses shape each medium as `track-count: N` **alongside** `track-list: []`
(present but empty).  `_parse_release_item` reads `len(track-list)` = 0 and never consults `track-count`,
so every search-resolved release is reported with 0 tracks.  The census script already proved the correct
precedence in `scripts/census_original.py:_extract_track_count` (use `track-list` length only when
non-empty; else fall back to `track-count`) — this session aligns the production path to that proven logic.

**Why this gates R3, not just hygiene.**  R2 froze C-TIER with `mb-search-resolved` as the entry criterion
for **99 of 107** clean dirs, keyed on *track-count reconciliation*.  With the bug live, that reconciliation
compares the local file count against 0 for the entire search-resolved population — C-TIER's most-populated
tier is behaviourally broken until this lands.  This is a Category-A fix on the critical path: **R1-F ✓ →
pre-R3 (this) → R3b → R5**.

**No design surface.**  The correct algorithm exists and is proven (census `_extract_track_count`).  This
session is mechanical alignment plus a regression test that pins the search-result shape — not a design
decision.  That is why `juncture-tier` opts down to `sonnet` (below).

## Verify gate

Touches `src/` and `tests/`; fully gated (100% branch coverage, strict mypy).  `/plan-run` re-discovers
these; stated here to document the gate:

- **VERIFY_TEST**: `~/.local/bin/tox -e test` — pytest, **100% branch coverage enforced** (`fail_under =
  100`).  The new fallback branch (`track-list` empty → `track-count`) needs an explicit regression test,
  or coverage fails.
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` — mypy strict on `src/ tests/`, **zero errors**.  No
  `Any`, no `cast()`.
- Full gate before ◆ close: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format +
  check_lint 10.00/10 + check_upgrade) must be green.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 ◆ | Fix `_parse_release_item` empty-`track-list` fallback to `track-count` | A | Sonnet | C-TIER (`mb-search-resolved` keys on track-count), `_discover._parse_release_item`, census `_extract_track_count` (proven reference) | `src/music_annotator/_discover.py`, `tests/unit/test_discover.py` |

`Cat`: A = substrate hardening (repairs C-TIER's search-tier denominator).  `Tier`: Sonnet — no open design
surface; the correct algorithm is proven in the census script.  `◆` = sub-track-final row (and the only
row).  **No juncture fires**: pre-R3 is a single-session Category-A fix; its ◆ hands off to the R3b adapter
shard (a separate `/plan-shard`), not to an adjudication fork.  No `@architect` inflection row — there is no
interface-design decision.

## Session detail

### S1 ◆ — fix `_parse_release_item` empty-`track-list` fallback

**Deliverable.**  `_parse_release_item` (`_discover.py`) derives per-medium track count using `track-list`
length **only when the list is non-empty**, falling back to `track-count` otherwise — matching
`scripts/census_original.py:_extract_track_count`.  The core change is the guard: `if isinstance(tl, list)
and tl:` (was `if isinstance(tl, list):`), routing the empty-list case into the existing `else` branch that
already reads `track-count`.  Verify the `else` still handles both the empty-list and absent-list cases
correctly after the guard tightens.

**≥1 KAT.**  `test_parse_release_item_empty_track_list_uses_track_count` — a search-shaped release dict with
`medium-list: [{"track-count": 12, "track-list": []}]`; assert the resulting `MBReleaseCandidate.tracks ==
12` (not 0).  Add the multi-medium case (search box set: two media each with `track-count` + empty
`track-list`) asserting the sum.  Retain the existing tests for non-empty `track-list` (uses length) and the
neither-present case (yields 0) — the fix must not regress them.

**Subtleties.**
- **Precedence asymmetry with `_score_toc_release`** (same file, ~line 371): `_score_toc_release` already
  prefers `track-count` first, then falls back to `track-list`.  After this fix, `_parse_release_item` and
  `_score_toc_release` agree on the empty-list case (both consult `track-count`).  Do **not** "unify" them
  into one helper in this session — that is a refactor with its own design surface; keep the fix surgical.
- **The census `_extract_track_count` stays as-is.**  It lives in `scripts/` (outside the src gate) and is
  the reference, not a shared dependency.  Do not import it into `_discover.py`; the production path owns its
  own (now-correct) copy.  Any dedupe is an R3-era refactor, not this fix.
- **Coverage.**  The tightened guard creates a newly-reachable `else` for the empty-list-present case.  The
  KAT must exercise it or `fail_under = 100` breaks.

**Deferrals.**  Any consolidation of the three track-count readers (`_parse_release_item` /
`_score_toc_release` / census `_extract_track_count`) into one shared helper is deferred — it is a refactor,
out of scope for a gating fix.  The spot-check gate on the `mb-search-resolved` population remains R3b's (per
J1), not this session.

## Cross-session contracts

### Consumed (frozen upstream — invalidation is a destructive-HALT)

- **C-TIER** (frozen at R2 S1, `AnnotationTier` + `classify_annotation_tier`): the `mb-search-resolved`
  tier's entry criterion is *track-count reconciliation*.  This session repairs the input to that
  reconciliation; it does **not** alter C-TIER itself.  If the fix appears to require changing the tier
  vocabulary or classifier signature, HALT — that means C-TIER was mis-frozen (a destructive signal), not
  that this fix grew.  **Flavour: test-enforced** at this consumer (the KAT pins the search-result shape).
- **C-NET-CORE / C-NET-TERM** (R1 / R1-F): `_parse_release_item` consumes results already routed through
  `_net`; this fix touches only the parsing of those results, not the transport.  Untouched.
- **`MBReleaseCandidate`** (models.py): the `tracks` field type and meaning are unchanged; only the value
  produced is corrected.

### Produced

- **None.**  This session freezes no new contract — it is scope-completeness on C-TIER's input path (the
  same shape as R1-F: a gating fix that consumes existing contracts and freezes nothing).  The regression
  test pins the search-result shape (`track-count` + empty `track-list`) as a test-enforced behavioural
  guard, but that is C-TIER's guarantee made whole, not a new contract.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Fix `_parse_release_item` empty-`track-list` fallback | pending | — | — |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **R-1 (the fix is provably correct — low design risk).**  `scripts/census_original.py:_extract_track_count`
  is the proven reference and its docstring already diagnoses the exact bug (lines 1104-1107).  Divergence
  from that logic in the fix is the signal to watch.  internal-continue if the fix matches the reference;
  surface only if the production path needs behaviour the reference lacks.
- **R-2 (do not scope-creep into a refactor — additive-reshard risk).**  The temptation to unify the three
  track-count readers is real and wrong for a gating fix.  If an executor consolidates them, that is scope
  drift: additive-reshard the dedupe into its own R3-era session; keep this one surgical.  The
  one-commit-title corollary is the guard — "Fix `_parse_release_item` …" is one title; "Fix and unify the
  track-count readers" is two.
- **R-3 (C-TIER is consumed, not touched — destructive-HALT boundary).**  If the fix seems to require editing
  `AnnotationTier` or `classify_annotation_tier`, stop: C-TIER is frozen (R2).  The fix lives entirely in
  `_discover.py` upstream of the classifier.  A change to the tier contract from this session is a
  destructive-HALT signal.
- **R-4 (coverage on the new branch).**  `fail_under = 100` means the newly-reachable empty-list `else` path
  must be tested.  A green `check_type` with a red `test` on coverage is the expected failure mode if the KAT
  is forgotten — not a surprise, a checklist item.

## Notes for executors

- **Tier routing.**  S1 is Sonnet (`@build`).  No juncture fires; the ◆ boundary hands off to the R3b
  whipper/MakeMKV adapter shard (a separate `/plan-shard` — the first J1-ordered R3 adapter, 52 clean dirs,
  highest identity confidence).  ROADMAP `juncture-tier: opus` stands at the roadmap level but this sub-track
  opts down to `sonnet` (see header): strong inner loop (lever 5) coincides with a fix that has no design
  surface.
- **Register: PEDAGOGY off** — thin mechanical docstrings per house style (Sphinx/PEP 257, 128-col).  The
  design rationale lives in this PLAN, not inline; a one-line code comment noting the search-result shape is
  sufficient at the fix site.
- **Invariants to preserve (do not regress):** the existing `_parse_release_item` behaviour for non-empty
  `track-list` (uses length) and the neither-present case (yields 0); C-TIER's classifier signature
  (untouched); `_net` transport routing (untouched).
- **Full gate before ◆ / commit:** `~/.local/bin/tox -m analyze` green (100% branch cov, mypy strict, pylint
  10.00/10, pyupgrade clean).
- **Sequencing:** this is the **last** R3 prerequisite.  R1-F is done (`e7370b7`).  On this ◆, all R3 gates
  are clear and R3b may be sharded.
- **Suggested `/plan-run` invocation:** `/plan-run halt-at-boundaries` — a single-session shard; halting at
  the ◆ hands the C-TIER-repaired search path to the user before R3b derives from it.
