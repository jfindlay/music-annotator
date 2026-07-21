<!-- juncture-tier: opus -->
<!-- sub-track: R3e (other-download provenance-label truthfulness) — ROADMAP critical-path; 3rd J1-ordered R3 adapter; 19 clean dirs; the source-variant collapse of R3a -->

# PLAN — R3e: other-download adapter (provenance-label truthfulness)

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Bring the 19 census-clean `other-download` dirs to their best annotation tier with a **truthful
provenance label**.  The R3a survey established the load-bearing fact: these dirs are **already
functionally ingested** by R3a's mechanism — they are ISRC-bearing, already recognised by
`is_presto_dir` (which keys on ISRC-presence alone, with no Presto-specific check), and already
ISRC-promoted to `full-mb-verified` via the C-ISRC ladder.  R3e adds **no new ingest path**.

The one genuinely-new concern is that the label is **wrong**.  The census taxonomy distinguishes
`presto` (booklet-PDF present) from `other-download` (ISRC-only, no PDF), but that PDF axis is
**offline and census-only** — the runtime recogniser never checked PDFs.  No Presto-specific runtime
artifact was confirmed across the 36 R3a dirs.  So `origin_source = "presto"` has, since R3a, been a
**generic ISRC-bearing-download** label misnamed after one vendor.  An operator auditing an
other-download or Amazon dir sees `"presto"` — a knowingly-false provenance string.

R3e resolves this by renaming the ISRC-presence label to the honest **`"download"`** (and
`is_presto_dir` → `is_download_dir`), reserving no vendor-specific label until a vendor-specific
runtime artifact is actually confirmed (deferred).  This is the **source-variant collapse J1
anticipated** ("R3e may collapse into R3a"), now confirmed total: same recogniser, same ladder, honest
label.  Freezes **C-DL**, which supersedes C-PRESTO's `"presto"` literal.

**Why this is a label rename, not a re-architecture.**  `origin_source` is a free-form `str` on
`ProvenanceSidecar` (not an enum), so the rename touches **no compiler contract** and **no tier /
identity / provenance-chain path**.  The C-ISRC promotion is label-independent by R3a's deliberate
design (it gates on the ISRC match itself, never on `origin_source`), so renaming the label cannot
change any tier outcome.  No persisted `"presto"` sidecars exist yet (R5 drain is operator-paced and
has not run), so there is no migration burden.

## Verify gate

Touches `src/` and `tests/`; fully gated (100% branch coverage, strict mypy).  `/plan-run`
re-discovers these; stated here to document the gate:

- **VERIFY_TEST**: `~/.local/bin/tox -e test` — pytest, **100% branch coverage enforced**
  (`fail_under = 100`).  The renamed recogniser's existing KATs must be renamed with it (no branch
  lost); the new other-download integration test adds an end-to-end path.
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` — mypy strict on `src/ tests/`, **zero errors**.
  No `Any`, no `cast()`.
- Full gate before ◆ close: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format +
  check_lint 10.00/10 + check_upgrade) green.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 ◆ | Rename ISRC-presence provenance label `presto`→`download`; add other-download integration test (freeze **C-DL**) | I | Sonnet | C-PRESTO (renamed by this row), C-ISRC, C-WHIP (mutual-exclusion unchanged), C-TIER | `src/music_annotator/_discover.py`, `src/music_annotator/_audit.py`, `tests/unit/test_discover.py`, `tests/integration/test_integration.py` |

`Cat`: I = integrative (cross-file rename riding entirely on frozen substrate + end-to-end proof).
`Tier`: **Sonnet** — the change is a mechanical rename of a free-form label plus an integration test;
the one design judgment (the C-PRESTO→C-DL flex) is adjudicated **at this shard boundary**, so the
executor applies a settled decision with no live design surface.  `◆` on S1 — sub-track-final; its
boundary hands off to R3d (a separate `/plan-shard`), not to an adjudication fork.

**Split/merge rationale (levers named).**  Roadmap estimated ~1-2 sessions; sharded to **1**.  The
work does not split at a contract-sharp boundary: the rename *is* the C-DL freeze and the integration
test *proves* the same freeze — one conceptual unit.  Splitting into a rename-only row and a test-only
row would fracture **below the irreducible floor (lever 2)** — both halves would be <60 LOC and the
test cannot meaningfully precede the label it asserts.  The **one-line-commit-title corollary** holds:
"Rename ISRC-presence provenance label `presto`→`download` and add other-download integration test" is
one commit-shaped title (a rename with its proof).  Net at the low end of ~1-2 because R3a delivered
the entire ingest path; R3e only corrects a string and proves the corpus flows through it.

## Session detail

### S1 ◆ — Rename ISRC-presence provenance label `presto`→`download`; add other-download integration test (freeze C-DL)

**Deliverable.**
- Rename `is_presto_dir` → **`is_download_dir`** in `_discover.py` (signature and body unchanged — the
  recognition heuristic is identical: ISRC-presence + no competing rip-provenance signature).
- Change the label written in `discover()`'s recognition block (`_discover.py:1056–1058`):
  `origin_source = "presto"` → **`origin_source = "download"`**; rename the log event
  `presto_dir_recognised` → `download_dir_recognised`.
- Update the `_audit.py` doc-comments (lines 311, 348) that describe the label: `"presto"` for
  ISRC-promoted entries → **`"download"`**.
- Rename the recogniser's existing KATs in `test_discover.py` to match (`is_download_dir`,
  `..._download`), and the C-PRESTO wiring KAT asserting `origin_source == "download"`.
- Add an **other-download integration test** in `test_integration.py`: an `other-download`-shaped
  fixture (ISRC-bearing FLAC bytes, **no booklet PDF**, no rip log, no disc info) → recognised
  (`is_download_dir` True; `origin_source == "download"`) → MB search resolution (mocked) → ISRC-match
  promotion to `full-mb-verified` → tags written and read back through the real mutagen path → journal
  + confirmation message correct.  Adapt or generalise the existing `test_presto_full_pipeline` fixture
  rather than duplicate it.

**≥1 KAT.**  The other-download integration test is the primary KAT (end-to-end, no internal-helper
patching per the integration convention).  Plus the renamed recogniser KATs
(`test_download_dir_recognised`, `test_no_isrc_not_download`, `test_disc_info_yaml_blocks_download`,
`test_whipper_log_blocks_download`, `test_empty_dir_not_download`) and the renamed wiring KAT
(`test_discover_passes_origin_source_download_to_run` asserting `origin_source == "download"`).

**Subtleties.**
- **The census script is out of scope.**  `scripts/census_original.py`'s `"presto"` provenance value
  is a *separate offline taxonomy axis* (PDF-based), not the runtime contract.  Do **not** touch it —
  the census meaning of `"presto"` (PDF-bearing) is correct in its own frame.  C-DL governs the
  **runtime** `origin_source` label only.
- **Rename, don't add.**  This is a pure rename of the single ISRC-presence path — do **not** add a
  vendor-branching recogniser (`presto` vs `amazon` vs `download`).  That was explicitly rejected at
  the shard boundary: no confirmed per-vendor runtime artifact and no per-vendor ingest difference
  across the 19-dir corpus.  Vendor recognition is a deferral (below).
- **No new branch.**  The rename must not change the recogniser's branch structure — `is_download_dir`
  has the identical two-condition body as `is_presto_dir`, so coverage KATs map 1:1.  A green
  `check_type` with red coverage would mean a KAT was dropped rather than renamed.
- **Integration fixture must not carry a PDF and must carry a *matching* ISRC** (source ISRC ∈ the
  mocked recording's `isrc_list`) so the promotion actually fires; a non-matching fixture would test
  only the search-resolved fallback and silently fail to exercise the full-verified path.

**Deferrals.**
- **Vendor-specific recognition** (booklet-PDF→a Presto label; Amazon COMM/PRIV tag frames→an Amazon
  label) is deferred until a vendor-specific runtime artifact is confirmed *and* a per-vendor ingest
  difference materialises.  Neither exists for the 19-dir corpus.  If a future census surfaces a
  vendor that needs distinct handling, that is an additive-reshard adding a `CensusSignal`-style
  branch, not a widening of C-DL.
- **Persisted-sidecar migration** is a non-issue now (no `"presto"` sidecars written yet; R5 undrained)
  and, if ever needed, is covered by the C-TIER monotonic-upgrade carve-out — an R5/Act-III-b concern,
  not R3e's.
- Bulk operator drain of the 19 dirs is R5 (operator-paced).  R3d adapter shard is the ◆ handoff.

## Cross-session contracts

### C-DL — generic download recognition + provenance label *(to be frozen at S1; supersedes C-PRESTO)*

What makes a source dir a generic (vendor-unidentified) download, and the `origin_source` label it
receives.  **Flavour: prose-enforced** (recognition heuristic) + **test-enforced** (KAT per branch).
Implemented S1 in `_discover.py` as `is_download_dir`, alongside `is_whipper_dir`.

**Supersedes C-PRESTO.**  C-PRESTO (R3a S2) froze the ISRC-presence recogniser under the name
`is_presto_dir` and the label `"presto"`.  C-DL renames both — `is_download_dir` and `"download"` —
with the **identical recognition heuristic**.  This is a label-truthfulness correction, not a
behavioural change: the recognition predicate, the whipper-precedence rule, and the
recognition-vs-evidence separation are all preserved verbatim.  `origin_source` is a free-form `str`,
so this is a prose+test flex, **not** a compiler contract change.

**Recognition heuristic (unchanged from C-PRESTO).**  A source dir is a **generic download** when
**both**:
1. at least one audio file yields a non-empty ISRC via `_read_isrc_tag`; **and**
2. it bears no competing strong rip-provenance signature — no whipper native log
   (`_find_whipper_log` → `None`) and no `00 - disc info.yaml` (which subsumes "no resolvable TOC",
   since `parse_disc_toc` reads exclusively from that file).

**`origin_source` value:** the literal string **`"download"`** (lowercase, exact), written once per
the `ProvenanceSidecar` idempotent-write rule.  **Whipper precedence:** whipper recognition runs
first; a dir matching both is whipper (C-WHIP mutual exclusion, unchanged).

**Recognition-vs-evidence separation (unchanged).**  C-DL governs the *provenance label*
(`origin_source`); the *tier promotion* is C-ISRC's evidence-gated ladder, which fires on the ISRC
**match against the selected medium** independent of `origin_source`.  A recognised download whose
ISRCs don't match the resolved release is `origin_source == "download"` but stays
`mb-search-resolved`.

- **Defined-in:** S1 (`_discover.py`: `is_download_dir` + the `"download"` label; `_audit.py`:
  doc-comment update).  **Consumed-by:** S1's own integration test (asserts `origin_source ==
  "download"`).  Downstream: R3d and any later download-class adapter reuse `is_download_dir`.
- **KATs that pin C-DL (S1):** `test_download_dir_recognised`, `test_no_isrc_not_download`,
  `test_disc_info_yaml_blocks_download`, `test_whipper_log_blocks_download`,
  `test_empty_dir_not_download`, `test_discover_passes_origin_source_download_to_run`, and the
  other-download integration test.

### Consumed (frozen upstream — invalidation is a destructive-HALT)

- **C-ISRC** (R3a S1): the ISRC-match tier promotion.  R3e **does not touch it** — the promotion is
  label-independent, so the rename cannot alter any tier outcome.  If the executor finds the rename
  requires editing the C-ISRC ladder, that is scope drift: **HALT**.  **Flavour:
  compiler+test-enforced.**
- **C-TIER** (R2 S1): the tier vocabulary + classifier signature.  Untouched.  **Flavour:
  compiler+test-enforced.**
- **C-WHIP** (R3b S1): whipper recognition + precedence.  S1 preserves the precedence ordering
  (whipper checked first) verbatim.  **Flavour: prose+test-enforced.**
- **C-PROV / C-MOVE + confirmation-provenance invariant** (repo `AGENTS.md`): unchanged — R3e adds no
  copy/verify path; `origin_source` is written by the existing provenance sidecar path.  **Flavour:
  prose+test-enforced.**

### Produced

- **C-DL** (S1), superseding C-PRESTO's label.  No other new contract.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Rename ISRC-presence provenance label presto→download; add other-download integration test | pending | — | C-DL |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **R-1 (this is a label rename, not an ingest change — do not add a new adapter path).**  R3a already
  ingests the R3e corpus whole.  If the executor finds themselves adding recognition branches, a new
  `CensusSignal`, or a tier path, that is scope drift into work R3a already did or into a deferred
  vendor-recognition feature: **internal-continue only if it is a pure rename**; anything more is an
  **additive-reshard** signal (surface at the ◆), not silent widening.
- **R-2 (vendor recognition is deferred, not forgotten).**  C-DL deliberately collapses all
  ISRC-bearing downloads to one label.  If a future census surfaces a vendor needing distinct handling
  (a confirmed runtime artifact *and* an ingest difference), that is an additive-reshard adding a
  vendor branch — never a widening of C-DL in place.
- **R-3 (wrong-pressing false-promotion — R5 watch item, carried down from R3a).**  ISRC-promoted
  downloads exit the spot-check population (`needs_spot_check=False`).  Because ISRC is
  recording-identity (not release-identity), a dir on a different pressing sharing recordings with the
  reconciled release could over-promote.  R3a's selected-medium match target is the correct guard, but
  the residual risk is an **R5-drain watch item**: wrong-pressing full-verified entries surfaced during
  drain are an additive-reshard (tighten C-ISRC) or destructive-HALT (if ISRC-match cannot license
  `full-mb-verified`).  This is a static-frame item; it lives in the ROADMAP Discoveries log too.
- **R-4 (census script is a separate frame).**  `scripts/census_original.py`'s `"presto"` value is
  PDF-based offline taxonomy, orthogonal to the runtime `origin_source` label C-DL governs.  An
  executor who "fixes" the census to say `"download"` has crossed a frame boundary — leave it.

## Notes for executors

- **Tier routing.**  S1 is **Sonnet** (`@build`).  There is no `@architect` inflection row: the one
  design judgment (the C-PRESTO→C-DL label flex) was adjudicated at the shard boundary and written into
  C-DL above.  ROADMAP `juncture-tier: opus` **stands** (user decision — the standing roadmap juncture
  tier is preserved even though this sub-track's single session is mechanical; the opus juncture has
  low adjudication load here but the roadmap-level correctness-criticality keeps it up).
- **Register: PEDAGOGY off** — thin mechanical docstrings per house style (Sphinx/PEP 257, 128-col).
  Update the `is_download_dir` docstring to drop Presto-specific wording; a one-line note that the
  recogniser matches any ISRC-bearing download suffices.
- **Invariants to preserve (do not regress):** C-ISRC's promotion path (untouched — label-independent);
  C-WHIP's whipper-precedence ordering; the recognition-vs-evidence separation; the
  `ProvenanceSidecar` idempotent-write rule on `origin_source`; the confirmation-provenance chain
  (no new sidecar path).
- **No `Any`, no `cast()`.**  No new `match/case`.
- **Full gate before ◆ / commit:** `~/.local/bin/tox -m analyze` green (100% branch cov, mypy strict,
  pylint 10.00/10, pyupgrade clean).
- **Sequencing:** R3e is the **3rd** J1-ordered R3 adapter and the source-variant collapse of R3a.  On
  the S1 ◆, R3e hands off to the **R3d** track-mismatch-tolerant adapter shard (a separate
  `/plan-shard` — 18 dirs, sub-classified edition vs structure, consumes C-S0).
- **Suggested `/plan-run` invocation:** `/plan-run` (run straight through) — a single mechanical
  Sonnet session with its judgment pre-settled needs no boundary or juncture halt; the ◆ close runs the
  full gate and hands off.  (If you prefer a checkpoint on the contract-flex, `/plan-run
  halt-at-boundaries` stops at the ◆ for review before handoff.)
