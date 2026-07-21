<!-- juncture-tier: opus -->
<!-- sub-track: R3d (track-mismatch operator-override) — 4th J1-ordered R3 adapter; last R3 code node; 18 in-mb-mismatch dirs; collapsed 3→1 by operator-policy (no auto-reconciliation) -->

# PLAN — R3d: track-mismatch operator-override

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Give the track-count-mismatch gate an **operator override** so the 18 `in-mb-mismatch` dirs stop
being silently skipped-and-forgotten.  Today `run()` hard-fails on any count mismatch
(`_pipeline.py:1554` `raise RuntimeError`; `_select_medium_with_reason` `ValueError` at :334);
`discover()` catches it, logs an error, and `continue`s — the dir stays in `Original/` with **no
record**.  R3d converts that hard fail into an interactive decision.

The load-bearing policy decision (operator, 2026-07-21): **a track-count mismatch cannot be
auto-reconciled.**  It means the local dir and the MB release disagree on structure — a genuine
edition/pressing difference, or a flat-local-vs-multi-disc-MB layout — and resolving it requires
**physical-medium inspection or a re-rip**, which is the operator's responsibility, not the
annotator's.  So R3d builds **no** multi-disc aggregation and **no** edition-vs-structure copy fork
(both were on the roadmap; both dropped).  It surfaces the discrepancy and lets the operator decide:

- **Accept** → ingest the selected (best) medium at the honest **`mb-partial`** tier; the operator
  has taken ownership of the discrepancy.  `audit` surfaces `mb-partial` entries for later review.
- **Decline** → skip; the dir stays in `Original/` for physical-medium handling on the operator
  backlog (R5 drain).

This follows two existing precedents verbatim: `_prompt_duration_warnings` (`_pipeline.py:400`,
proceed/abort on duration drift) and `confirm_disc` (`_discover.py:162`, accept/override/abort on
heuristic medium selection).  R3d adds a third override of the same shape.

**Why this is an override, not a re-architecture.**  The `mb-partial` tier, the
`CensusSignal.MISMATCH` signal, and the `classify_annotation_tier(MISMATCH) → (MB_PARTIAL, False)`
arm **already exist** — R2 over-specified C-TIER for exactly this (`models.py:53,98,120`), and
`_audit.py:398` already counts `mb-partial`.  The code comment at `_pipeline.py:1727` even documents
that `mb-partial` is "not reachable from run()" today.  R3d makes it reachable.  Freezes **C-OVR**
(the `confirm_count_mismatch` Protocol method + accept→`MISMATCH` wiring); touches **no** C-TIER
contract.

## Verify gate

Touches `src/` and `tests/`; fully gated (100% branch coverage, strict mypy).  `/plan-run`
re-discovers these; stated here to document the gate:

- **VERIFY_TEST**: `~/.local/bin/tox -e test` — pytest, **100% branch coverage enforced**
  (`fail_under = 100`).  The new override has two branches (accept / decline) plus dry-run
  suppression; each needs an explicit test.
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` — mypy strict on `src/ tests/`, **zero errors**.
  No `Any`, no `cast()`.  The new Protocol method must be typed and implemented on every
  `DiscoverUI` double.
- Full gate before ◆ close: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format +
  check_lint 10.00/10 + check_upgrade) green.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 ◆ @architect | Add operator override to the track-count-mismatch gate; ingest accepted mismatches at `mb-partial` (freeze **C-OVR**) | I | Opus | C-TIER (`mb-partial` + `MISMATCH`, unchanged), C-WHIP (whipper precedence unchanged), C-PROV/C-MOVE (copy loop untouched) | `src/music_annotator/_discover.py`, `src/music_annotator/_pipeline.py`, `tests/unit/test_discover.py`, `tests/unit/test_pipeline.py`, `tests/integration/test_integration.py` |

`Cat`: I = integrative (an interactive gate + Protocol extension riding entirely on frozen C-TIER
substrate + end-to-end proof).
`Tier`: **Opus / `@architect`** — the one live design surface is the `confirm_count_mismatch`
Protocol signature (a compiler-enforced contract every UI double must satisfy) *and* the accepted-
override plan-build subtlety (below), which needs judgment against live code.  `◆` on S1 —
sub-track-final; its boundary closes the R3 code arc and hands off to R5 (operator drain), not to
another adapter shard.

**Split/merge rationale (levers named).**  Roadmap estimated ~3 sessions (two-sub-class adapter with
multi-disc reconciliation); collapsed to **1** by the operator-policy decision that eliminated the
aggregation-copy path and the edition/structure fork (lever 2 — the irreducible work shrank, it was
not fractured).  The remaining work — Protocol method + terminal prompt + gate rewrite + MISMATCH
wiring + tests — is **one conceptual unit**: splitting the Protocol method into its own row would
leave dead, untestable interface code until its consumer landed (fracturing below the floor, lever
2), and the two existing precedents (`_prompt_duration_warnings`, `confirm_disc`) were each landed as
one unit.  **One-line-commit-title corollary** holds: "Add operator override to the track-count-
mismatch gate; ingest accepted mismatches at `mb-partial`" is one commit-shaped title.  Ambient
complexity is high (lever 1 — deep `run()`), but the change is localized to one gate + one Protocol
method (~150 LOC), so lever 1 keeps it tight rather than splitting it.

## Session detail

### S1 ◆ @architect — Add operator override to the track-count-mismatch gate (freeze C-OVR)

**Deliverable.**
- Add **`confirm_count_mismatch`** to the `DiscoverUI` Protocol (`_discover.py:70`) and implement it
  on `TerminalDiscoverUI` (`_discover.py:126`), following `confirm_disc`'s structure.  Proposed
  signature (the C-OVR freeze — `@architect` confirms/adjusts):
  `def confirm_count_mismatch(self, src_dir: Path, release: MBRelease, selected_medium: MBMedium,
  n_src: int, n_medium: int, diagnostic: str) -> bool` — returns `True` to accept (ingest at
  `mb-partial`), `False` to decline (skip).  `diagnostic` is the human-readable edition-vs-structure
  context string.
- Rewrite the mismatch gate at `_pipeline.py:1554`: instead of an unconditional `raise RuntimeError`,
  when `ui is not None and not dry_run` call `ui.confirm_count_mismatch(...)`; on `False` (or when
  `ui is None` / dry-run — preserve the non-interactive hard-fail contract) keep the raise; on `True`
  set a flag that forces `census_signal = CensusSignal.MISMATCH` and proceed.
- Handle the `_select_medium_with_reason` `ValueError` path (`_pipeline.py:334`, multi-disc no-match)
  under the same override — a multi-disc release where no single medium matches `n_src` is the
  structure-mismatch case and must reach the same prompt, not die at medium selection.
- Derive the **edition-vs-structure diagnostic** for the prompt: `shape.disc_subdirs` / local
  subdir presence + the local-vs-MB count ratio (structure = flat-local vs multi-disc-MB or MB total
  ≠ local; edition = single-medium count disagreement).  Display-only context; does **not** branch
  behavior.
- Force `census_signal = CensusSignal.MISMATCH` on an accepted override so
  `classify_annotation_tier` yields `(MB_PARTIAL, False)`; ingest proceeds against the selected/best
  medium's `copy_subset`.
- Add the KATs (below) and an **end-to-end mismatch integration test** in `test_integration.py`.

**≥1 KAT.**  The mismatch integration test is the primary KAT (end-to-end, no internal-helper
patching per the integration convention): a count-mismatched fixture → gate fires →
`confirm_count_mismatch` stub returns `True` → ingest at `mb-partial` → sidecar `annotation_tier ==
"mb-partial"` → `audit` surfaces it.  Plus unit KATs: `test_count_mismatch_accept_ingests_partial`,
`test_count_mismatch_decline_skips`, `test_count_mismatch_dry_run_still_raises`,
`test_count_mismatch_no_ui_still_raises`, `test_multidisc_no_match_reaches_override`,
`test_confirm_count_mismatch_terminal_accept` / `_decline` (terminal-prompt parsing).

**Subtleties.**
- **The accepted-override plan-build is the one genuine design judgment (why `@architect`).**  The
  gate at :1554 fires when `len(src_files) != len(copy_subset)`.  On accept, the copy/tag/verify loop
  operates on `copy_subset` (selected medium) — but if `n_src < n_medium` or `n_src > n_medium`, the
  downstream plan-build (`_build_copy_plan` / `tags_map` zip) will itself hit the count disparity.
  The executor must decide, against live code, how the accepted partial maps: copy the
  `min(n_src, n_medium)` positionally-aligned tracks, or copy all `n_src` against the first `n_src`
  medium tracks, or another rule.  **This is the C-OVR behavioral core** — resolve it so the
  `mb-partial` ingest is deterministic and the integration test pins it.  Do **not** guess silently;
  if the mapping is ambiguous beyond a positional min, surface it (additive-reshard signal).
- **Preserve the non-interactive contract.**  `dry_run` and `ui is None` must keep the hard-fail
  raise — automation must not hang on a prompt and must not silently ingest partials.  Mirror
  `_prompt_duration_warnings`' dry-run skip and `confirm_disc`'s `ui is not None` guard exactly.
- **Whipper precedence unchanged (C-WHIP).**  A whipper dir that also mismatches still routes through
  whipper recognition first; the override is orthogonal to `origin_source`.
- **`mb-partial` is monotonic-upgrade safe (C-TIER carve-out).**  Writing `mb-partial` must respect
  the write-once-monotonic rule; a dir already at a higher tier must not be lowered by an accepted
  mismatch.  Reuse the existing `annotation_tier_rank` comparison path.
- **Copy-provenance chain untouched (C-PROV/C-MOVE).**  The override gates *entry to* the copy loop;
  it must not alter the copy/tag/verify/journal ordering or the confirmation-provenance invariant.
  An accepted partial still runs the full verify path for the tracks it does copy.

**Deferrals.**
- **Multi-disc aggregation / structure reconciliation.**  Explicitly not built — the operator handles
  structure mismatches against the physical media (R5).  If a future need for automatic flat→multi-
  disc mapping materializes, that is an additive-reshard consuming C-S0, never a widening of C-OVR.
- **A persisted mismatch registry / worklist artifact.**  R3d relies on the existing `audit`
  `mb-partial` enumeration to surface accepted partials; declined dirs are simply left in `Original/`.
  A dedicated "dirs I declined" registry is a possible R5 convenience, deferred.
- **The edition-vs-structure diagnostic as a persisted field.**  It is prompt-display context only;
  not written to the sidecar.  Persisting it is deferred (no consumer yet).

## Cross-session contracts

### C-OVR — track-count-mismatch operator override *(FROZEN S1 — juncture-design 2026-07-21)*

The operator decision surface for a track-count mismatch, and the tier an accepted mismatch
receives.  **Flavour: compiler-enforced** (the `confirm_count_mismatch` Protocol method — every
`DiscUI`/`DiscoverUI` implementation and test double must implement it or mypy fails) +
**test-enforced** (KAT per branch: accept / decline / dry-run / no-ui / multi-disc-no-match).

**Frozen signature (compiler contract).**

```python
def confirm_count_mismatch(
    self,
    src_dir: Path,
    release: MBRelease,
    selected_medium: MBMedium | None,
    n_src: int,
    n_medium: int,
    diagnostic: str,
) -> bool: ...
```

Returns `True` to accept (proceed at `mb-partial`), `False` to decline (skip).

**Freeze-time adjustments to the PLAN's proposed signature (against live code):**

1. **`selected_medium` is `MBMedium | None`, not `MBMedium`.**  The multi-disc no-match path
   (`_select_medium_with_reason` raises `ValueError` at `_pipeline.py:334` *before* a medium is
   returned) has **no** selected medium at the raise site.  To route that path to the same prompt
   the parameter must admit `None`.  On the count-mismatch gate path (`_pipeline.py:1554`)
   `selected_medium` is always bound and non-`None` (guaranteed by the `if selected_medium is None:
   raise` guard at `_pipeline.py:1519`); on the no-match path it is `None` (or a best-effort
   nearest-count medium if the executor chooses to compute one for display — see the no-match
   handling below).  `n_medium` carries the count regardless (`0` or the best-medium count when
   `selected_medium is None`), so the prompt can render without dereferencing a possibly-`None`
   medium.

2. **The method lands on TWO protocols, not one.**  The PLAN named only
   `DiscoverUI` (`_discover.py:70`).  But the gate lives in `run()`, whose `ui` parameter is typed
   `DiscUI` (`_pipeline.py:108`) — a **deliberately separate structural-subset Protocol** kept in
   `_pipeline.py` to avoid the `_discover → run` circular import (documented at `_pipeline.py:113`).
   `run()` can only call `confirm_count_mismatch` if it is declared on `DiscUI`.  Freeze:
   **`confirm_count_mismatch` is added to both `DiscUI` (`_pipeline.py:108`, the callable contract)
   and `DiscoverUI` (`_discover.py:70`, the full surface), and implemented on `TerminalDiscoverUI`
   (`_discover.py:126`) mirroring `confirm_disc`.**  Every test double for *either* protocol must
   implement it or mypy fails.  This is an interface-surface detail within `@architect`'s
   confirm/adjust latitude — no scope change.

**Gate behavior (prose+test contract).**  Two entry points converge on one prompt:

- **Single-medium / disc-override count mismatch** (`_pipeline.py:1554`, condition
  `len(src_files) != len(copy_subset)`): if `ui is not None and not dry_run`, call
  `ui.confirm_count_mismatch(src_dir, release, selected_medium, len(src_files), len(copy_subset),
  diagnostic)`.  Accept → set an `accepted_mismatch` flag, force `census_signal =
  CensusSignal.MISMATCH`, and truncate to the positional-min subset (below); decline → the original
  `raise RuntimeError`.  `ui is None` or `dry_run` → the original `raise RuntimeError`
  (non-interactive contract preserved).

- **Multi-disc no-match** (`_pipeline.py:334` `ValueError` out of `_select_medium_with_reason`, seen
  at the `_pipeline.py:1505` call site): wrap the call in `try/except ValueError`.  On `ValueError`,
  if `ui is not None and not dry_run`, choose a **best medium** for ingest (the medium whose
  `len(track_list)` is nearest to `n_src`; ties → lowest `position`) and call
  `confirm_count_mismatch(..., selected_medium=best_medium, n_src=len(src_files),
  n_medium=len(best_medium.track_list), diagnostic=...)`.  Accept → proceed with `best_medium` as
  `selected_medium` under the same `MISMATCH` + positional-min rule; decline → re-`raise` the
  original `ValueError`.  `ui is None` or `dry_run` → re-`raise` the original `ValueError`.  (The
  executor MAY instead pass `selected_medium=None` and defer best-medium choice into the prompt-body
  return contract, but the frozen decision is: the pipeline picks the nearest-count medium so the
  ingest is deterministic and the KAT can pin it.  Nearest-count is display-and-ingest; the operator
  keystroke is the authority.)

**Positional-min mapping rule (R-3 behavioral core — FROZEN).**  On an accepted override the ingest
copies exactly `k = min(len(src_files), len(selected_medium.track_list))` tracks, positionally
aligned: source file `i` ↔ selected-medium track `i` for `i` in `range(k)`.  This is forced against
live code because **three loops in `run()` index positionally and will `IndexError` otherwise**:

- copy-plan build (`_pipeline.py:1653`): iterates `copy_subset` and reads `src_files[copy_subset_pos]`
  → `IndexError` when `n_src < n_medium`.
- embedded-MBID tier probe (`_pipeline.py:1734`): `_read_recording_id_tag(f) for f in src_files`
  then membership-tests against the medium track-id set — safe on length but semantically must be
  scoped to the copied `k`.
- ISRC tier probe (`_pipeline.py:1745`): `src_files[i]` vs `selected_medium.track_list[i]` for
  `range(len(src_files))` → `IndexError` when `n_src > n_medium`.

Freeze: on an accepted mismatch, truncate **both** `src_files` and `copy_subset` to the first `k`
(build a `copy_subset` restricted to the first `k` selected-medium tracks, and slice `src_files[:k]`
for the plan/tier probes) so every positional loop stays in-bounds and the copied set is
deterministic.  The `mb-partial` sidecar tier records that the ingest is partial; `audit` surfaces
it.  The dropped tail (whichever side is longer) is **not** copied and **not** journaled —
consistent with "operator owns the discrepancy."  This is within R-2/R-3's anticipated "positional
min" envelope — **not** an additive-reshard: no aggregation, no smart merge, one deterministic rule.

**Diagnostic string (display-only, not persisted).**  Human-readable edition-vs-structure context:
whether local layout is flat vs the release is multi-disc (structure mismatch), or a single-medium
count disagreement (edition mismatch), plus the `n_src` vs `n_medium` counts.  Does not branch
behavior; not written to the sidecar (deferral honored).

**Tier outcome (consumes C-TIER unchanged).**  Accept → `census_signal = CensusSignal.MISMATCH` →
`classify_annotation_tier(MISMATCH)` → `(MB_PARTIAL, False)` (verified live at `models.py:120`).
Written through the existing monotonic-upgrade path (`_pipeline_io.py:1538`), so a dir already at a
higher tier is not lowered (C-TIER carve-out honored automatically — no new code needed).  No new
tier, no new signal, no new classifier arm — R2 froze all three.  An adapter that appears to need a
new *tier* here is a destructive-HALT signal that C-TIER was mis-frozen (per the R3b boundary
discovery).

- **Defined-in:** S1 (`_discover.py`: `confirm_count_mismatch` on `DiscoverUI` Protocol +
  `TerminalDiscoverUI`; `_pipeline.py`: `confirm_count_mismatch` on `DiscUI` Protocol + gate rewrite
  + no-match `try/except` + positional-min truncation + `MISMATCH` wiring).  **Consumed-by:** S1's
  own integration test and every `DiscUI`/`DiscoverUI` test double (compiler-forced to implement the
  method on both protocols).  Downstream: none within this roadmap — R3d is the last R3 code node.
- **KATs that pin C-OVR (S1):** `test_count_mismatch_accept_ingests_partial` (should cover **both**
  min directions: `n_src < n_medium` and `n_src > n_medium`, since each exercises a different
  positional loop's `IndexError` guard and each needs branch coverage),
  `test_count_mismatch_decline_skips`, `test_count_mismatch_dry_run_still_raises`,
  `test_count_mismatch_no_ui_still_raises`, `test_multidisc_no_match_reaches_override` (+ a
  dry-run/no-ui counterpart so the no-match re-`raise ValueError` branch is covered),
  `test_confirm_count_mismatch_terminal_accept`, `test_confirm_count_mismatch_terminal_decline`, and
  the mismatch integration test.

### Consumed (frozen upstream — invalidation is a destructive-HALT)

- **C-TIER** (R2 S1): the tier vocabulary (`AnnotationTier`), `CensusSignal.MISMATCH`, and
  `classify_annotation_tier`.  R3d **consumes them unchanged** — it wires an existing signal, adds no
  tier and no classifier arm.  If the executor finds they must edit `AnnotationTier` or add a
  `CensusSignal`, that is scope drift: **HALT**.  **Flavour: compiler+test-enforced.**
- **C-WHIP** (R3b S1): whipper recognition + precedence.  Unchanged; the override is orthogonal to
  `origin_source`.  **Flavour: prose+test-enforced.**
- **C-S0** (aggregation spans media): **not consumed** — R3d builds no aggregation path.  Named here
  only to record that the roadmap's original "R3d-structure consumes C-S0" plan was dropped.
- **C-PROV / C-MOVE + confirmation-provenance invariant** (repo `AGENTS.md`): unchanged — the
  override gates entry to the copy loop; the copy/tag/verify/journal ordering and the
  confirmation-provenance chain are untouched.  **Flavour: prose+test-enforced.**

### Produced

- **C-OVR** (S1).  No other new contract.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Add operator override to the track-count-mismatch gate; ingest accepted mismatches at mb-partial | interface-frozen (impl pending) | — | C-OVR (design frozen 2026-07-21) |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **R-1 (this wires an existing signal — do not add a tier or a CensusSignal).**  `mb-partial`,
  `CensusSignal.MISMATCH`, and the classifier arm all exist (R2 over-specified C-TIER).  If the
  executor finds themselves editing `AnnotationTier` or `classify_annotation_tier`'s arms, that is a
  **destructive-HALT** signal that C-TIER was mis-frozen — surface at the ◆, do not widen in place.
- **R-2 (no auto-reconciliation — the operator owns the discrepancy).**  A track-count mismatch is a
  physical-world fact (edition/pressing/structure) the agent cannot adjudicate.  If the executor
  reaches for a multi-disc aggregation or a positional-guess "smart merge" beyond the deterministic
  min-map fixed in S1, that is scope drift into the deferred R3d-structure work: **internal-continue
  only for the settled override + min-map**; anything more is an **additive-reshard** signal.
- **R-3 (accepted-partial plan-build is the live design point).**  The one judgment the ◆ @architect
  session must resolve against live code: how `n_src` files map onto the selected medium's
  `copy_subset` when the counts differ.  Fix it deterministically and pin it with the integration
  test; if the mapping is ambiguous beyond a positional min, that is an **additive-reshard** signal,
  not a silent choice.
- **R-4 (wrong-pressing risk is bounded here, unlike ISRC promotion).**  Unlike C-ISRC's silent
  full-verified promotion (R3a watch item), an accepted mismatch lands at `mb-partial` +
  audit-surfaced and required an explicit operator keystroke — the operator has already accepted the
  discrepancy.  The residual risk is operator error at the prompt, not silent over-promotion; the
  diagnostic string is the mitigation.
- **R-5 (non-interactive contract).**  Automation (dry-run, no-UI) must keep the hard fail — a prompt
  that hangs a batch run or a silent partial ingest under automation would be a regression.  This is
  a test-enforced invariant (`test_count_mismatch_dry_run_still_raises`, `_no_ui_still_raises`).

## Notes for executors

- **Tier routing.**  S1 is **Opus / `@architect`** — the live design surface is the C-OVR Protocol
  signature freeze *and* the accepted-partial plan-build mapping (R-3).  Do not delegate the mapping
  decision to a mechanical pass.
- **Register: PEDAGOGY off** — thin mechanical docstrings per house style (Sphinx/PEP 257, 128-col).
  The `confirm_count_mismatch` docstring mirrors `confirm_disc`'s register (params, return, the
  accept/decline/abort semantics).
- **Invariants to preserve (do not regress):** C-TIER's tier vocabulary and classifier (consumed
  unchanged — no new tier/signal); C-TIER's monotonic-upgrade carve-out on `annotation_tier`;
  C-WHIP's whipper precedence; the C-PROV/C-MOVE copy/verify ordering and the confirmation-provenance
  chain (the override gates entry only); the non-interactive hard-fail contract under dry-run/no-UI.
- **No `Any`, no `cast()`.**  The gate rewrite may introduce a `match/case` on the accept/decline
  outcome — if so, include the `case _: # pragma: no cover` arm per house convention.
- **Full gate before ◆ / commit:** `~/.local/bin/tox -m analyze` green (100% branch cov, mypy strict,
  pylint 10.00/10, pyupgrade clean).
- **Sequencing:** R3d is the **4th** J1-ordered R3 adapter and the **last R3 code node**.  On the S1
  ◆, the R3 code arc closes; handoff is to **R5** (operator drain of `Original/`, no agent sessions)
  and eventually **J3 → R6**.  The post-R3 structural-audit trigger (ROADMAP Junctures note) becomes
  eligible after this ◆.
- **Suggested `/plan-run` invocation:** `/plan-run halt-at-boundaries` — a single Opus `@architect`
  session with one live design decision (the C-OVR signature + accepted-partial mapping) benefits
  from a boundary halt so the freeze and the mapping rule can be reviewed before the ◆ closes the R3
  arc.  (If you prefer to run straight through, plain `/plan-run` also works — it is one session.)
