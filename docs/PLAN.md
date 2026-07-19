<!-- juncture-tier: opus -->
<!-- sub-track: R1-F (search/disc-ID → _net) — BACKLOG-resident completion of R1; no new DAG node -->

# PLAN — R1-F: migrate MB search/disc-ID calls onto `_net`; retire `_mb_retry`/`_mb_call`

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

R1 collapsed MB-data, CAA, and AcoustID retrieval onto the `_net` core, but its session list never
enrolled two `_discover.py` search/disc-ID call sites.  They remain on the **legacy**
`_mb_retry`/`_mb_call` path with the codebase's **last** `"404" in str(exc)` scrape.  This sub-track
closes that gap so the "every remote fetch routes through `_net.retrieve()` with a structured
classifier" property holds **literally** — the property every R3 adapter leans on from day one.

After this row: `_mb_retry` and `_mb_call` are deleted; musicbrainzngs's role shrinks to pure XML
parsing (`mbxml.py`) plus the two surviving monkey-patches (`_patched_safe_read`,
`_patched_parse_recording`); transport, retry, and polite-delay are entirely owned by `_net`.

This is a **substrate-completing** migration against an already-frozen contract — it designs no new
interface.  It reuses `_mb_data_classify` (frozen in R1/S2), whose `404 → NO_DATA` verdict already
subsumes the hand-rolled `"404" in str(exc)` branch in `_toc_lookup_mb_releases`.  No `@architect`
juncture fires.

## Verify gate

Discovered from `pyproject.toml` (`/plan-run` re-discovers; stated here to document the gate):

- **VERIFY_TEST**: `~/.local/bin/tox -e test` → `pytest tests/` with `--cov=music_annotator
  --cov-report=term-missing`; **100% branch coverage enforced** (`branch = true`, `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` → `mypy src/ tests/` (strict, `python_version =
  3.12`).
- Full gate before the row is `done`: `~/.local/bin/tox -m analyze` (build · test · check_type ·
  check_format · check_lint 10.00/10 · check_upgrade).  No `Any`, no `cast()` in source.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 ◆ | Migrate MB search/disc-ID calls onto `_net`; retire `_mb_retry`/`_mb_call` | A | Sonnet | C-NET-CORE, C-NET-TERM (frozen R1/S1) | `src/music_annotator/_discover.py`, `src/music_annotator/_mb_api.py`, `tests/unit/test_discover.py`, `tests/unit/test_mb_helpers.py` |

`Cat`: A = substrate.  `◆` = sub-track-final row (trivially — the sub-track is one row).  No
`@architect` marker: the row freezes **no new contract** and consumes only frozen ones, so no
interface-design juncture fires.  `test_mb_helpers.py` is listed because deleting
`_mb_retry`/`_mb_call` removes the functions its `TestMbRetry` class (if it still targets them
directly) exercises — the executor reconciles or removes those tests.

## Session detail

### S1 ◆ — search/disc-ID migration + legacy-retry retirement

**Deliverable.** Route both `_discover.py` MB-transport call sites through `_net.retrieve()` with the
frozen `_mb_data_classify`, then delete the now-unused `_mb_retry`/`_mb_call`:

1. **`_search_mb_releases`** (`_discover.py:423`) — replace the `@_mb_retry` inner `_call` +
   `_mb_call(_call)` with `retrieve(_call, policy)` where `policy = NetPolicy(classify=_mb_data_classify,
   event="mb_search", log_fields={"query": query})`.  `mb.search_releases` returns a `{"release-list": …}`
   dict; on `retrieve` returning `None` (authoritative 404 / no-data), map to `{}` so the existing
   `raw.get("release-list", [])` in `search_releases_by_dir` yields no candidates.
2. **`_toc_lookup_mb_releases`** (`_discover.py:292`) — replace the `@_mb_retry` + `try/except
   mb.ResponseError` `"404" in str(exc)` block with `retrieve(_call, policy)` using the same classifier;
   `event="mb_discid"`.  The classifier's `404 → NO_DATA` verdict returns `None`; map `None → []`
   (today's no-matches return).  **Delete the `str(exc)` scrape entirely** — this is the last one in the
   codebase.
3. **Delete `_mb_retry` and `_mb_call`** from `_mb_api.py` once both callers are migrated; update
   `_discover.py`'s `from music_annotator._mb_api import … _mb_call, _mb_retry …` line (`_discover.py:22`)
   to drop them and add `retrieve`, `NetPolicy` (and `_mb_data_classify` — all bound in `_mb_api`).
4. **`__init__.py`**: `_mb_retry`/`_mb_call` are not in `__all__` (private, `_`-prefixed) — confirm no
   public-surface change.  If either is re-exported anywhere, update `__all__`.

**KAT (≥1 required).** `test_discover.py`:

- `TestSearchReleasesByDir` / `TestSearchMbReleasesPoliteDelay`: transient (503 / OSError) retries then
  raises after exhaustion (was: same, via `_mb_retry`); the polite-delay assertion re-points from
  `_mb_call`'s `time.sleep(1)` to `retrieve`'s `policy.polite_delay_s` (default 1.0) on the success path.
- `TestTocLookupMbReleases`: the **404 → `[]`** case (previously the `"404" in str(exc)` branch) now
  flows through `_mb_data_classify` → `NO_DATA` → `retrieve` returns `None` → mapped to `[]`.  This KAT
  pins that the hand-rolled scrape and the frozen classifier produce the **same** observable no-data
  result.  Also: a non-404 `ResponseError` (FATAL) raises; a 5xx retries-then-raises.

These are KATs because they pin C-NET-TERM (the universal terminal rule) applied to the two search
paths — the deliverable's contract is behavioural equivalence of the frozen classifier with the
retired scrape, plus loss-of-nothing on the retry/exhaustion semantics.

**Subtleties.**

- **Patch-target discipline** (see Notes for executors): `TestSearchReleasesByDir` patches
  `music_annotator._discover._search_mb_releases` (the wrapper) — unaffected.  Tests that patch
  `mb.search_releases` / `mb.get_releases_by_discid` or assert on `_mb_call`'s `time.sleep` must move to
  the `_net` binding: after migration the sleep is inside `_net.retrieve`, so a `time.sleep` assertion
  patches `music_annotator._net.time.sleep`, not `_mb_api` / `_discover`.  Update all such sites in this
  session.
- **`_toc_lookup_mb_releases` two-shape response** (`{"disc": {"release-list": …}}` exact vs
  `{"release-list": …}` fuzzy) is orthogonal to transport — preserve the post-fetch shape-handling
  verbatim; only the fetch + retry + 404 layer changes.
- **Exhaustion re-raise type.** Per C-NET-CORE, `retrieve` re-raises the *last* caught exception on
  exhaustion (not a synthesised `RuntimeError`).  Any surviving `TestMbRetry`-style assertion matching a
  `RuntimeError("… after retries")` string must be reconciled against the frozen re-raise behaviour —
  the raise-on-exhaustion contract is frozen; the exact exception type aligns with the surviving tests.
- **`_patched_safe_read` stays** (MB-data only; removal gated on mbngs2-1 — do not touch).

**Deferrals.** None internal to this sub-track.  The musicbrainzngs2 migration (`_patched_safe_read`
upstreaming / removal) and the post-R3 structural-audit trigger stay in BACKLOG.

## Cross-session contracts

### Consumed (frozen upstream — invalidation is a destructive-HALT)

- **C-NET-CORE / C-NET-TERM** (prior `PLAN` R1, frozen 2026-07-18 at R1/S1; `src/music_annotator/_net.py`).
  This sub-track consumes them unchanged: `retrieve(fetch, policy) -> T | None`, the `NetPolicy`
  dataclass, and the three-outcome `RetryDecision` terminal rule.  `None` (NO_DATA) maps to `{}`
  (search) / `[]` (disc-ID) at the two call sites — the same caller-side sentinel mapping every R1
  migration used.  **Freezes no new contract.**
- **`_mb_data_classify`** (frozen R1/S2; `_mb_api.py:279`) — reused verbatim; its `404 → NO_DATA` and
  `503/500/429/307 → RETRY` / `OSError → RETRY` verdicts already cover both search call sites.  No new
  classifier is written.
- **C-PROV / C-MOVE** (move/verify/journal provenance, `docs/NOTES.md`) and the defensive-download /
  confirmation-provenance invariants (repo `AGENTS.md`) — the migrated calls must preserve the two-layer
  retry + 1 req/s polite-delay posture (now via `NetPolicy.polite_delay_s`) and the
  raise-on-cannot-determine contract.

*(No new contract subsection is authored here because the single row freezes nothing new — a
deliberate consequence of R1-F being a substrate-completion, not a substrate-definition.)*

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | search/disc-ID → `_net`; retire `_mb_retry`/`_mb_call` | pending | — | — |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **D-1 (patch-target relocation — S1).** Deleting `_mb_call` moves the polite-delay `time.sleep` from
  `_mb_api._mb_call` into `_net.retrieve`.  Any test asserting the 1 req/s delay must re-point its
  `time.sleep` patch to `music_annotator._net`.  `/plan-run`: treat as **internal-continue** (mechanical
  patch-site update within the frozen contract), not a reshard.
- **D-2 (exhaustion exception type — S1).** If a surviving legacy test asserts `RuntimeError("… after
  retries")`, reconcile against C-NET-CORE's re-raise-last-exception behaviour (the type may now be
  `mb.ResponseError`).  The *raise-on-exhaustion* contract is frozen; the assertion string is the
  executor's to align.  Not a design change — **internal-continue**.
- **R-1 (`_mb_retry`/`_mb_call` residual references).** Before deleting, confirm no caller outside the
  two migrated sites imports them (search `_mb_retry`, `_mb_call` across `src/` + `tests/`).  R1's ledger
  and BACKLOG state these two `_discover.py` sites are the *sole* survivors — verify at execution before
  removal.  If a third caller surfaces, that is an **additive-reshard** signal (unenrolled scope).

## Notes for executors

- **Tier routing.** S1 is Sonnet (`@build`) — mechanical migration against the frozen C-NET-CORE /
  C-NET-TERM contract; no interface designed, so **no `@architect` juncture fires** despite the
  roadmap-fixed `juncture-tier: opus` (that governs junctures when they occur; this shard has none).
- **Register: PEDAGOGY** off (production code, house style — thin mechanical docstrings per AGENTS.md).
- **Invariants to preserve.** No `Any`, no `cast()` in source.  100% branch coverage — the migrated
  `None`-mapping branches (`None → {}`, `None → []`) and any `match/case` need explicit tests.  `from
  __future__ import annotations` already present.  The `# type: ignore[no-any-return]` on the `mb.*`
  calls stays (musicbrainzngs is untyped) — keep it minimal, do not widen to `Any`.
- **Test patch-target discipline.** Patch names where *bound*.  After migration the retry / sleep lives
  in `_net.retrieve`; re-point `time.sleep` / retry assertions to `music_annotator._net`.  The wrapper
  patch targets (`music_annotator._discover._search_mb_releases`,
  `music_annotator._discover._toc_lookup_mb_releases`) are unaffected.
- **Deletion ordering.** Migrate BOTH call sites and update the `_discover.py:22` import BEFORE deleting
  `_mb_retry`/`_mb_call` — the functions are live until the second caller moves.
- **Suggested first `/plan-run` invocation** (single-row, no juncture, frozen contract → low risk, but
  the shard pattern for a "follow-on completion" is unproven): `/plan-run halt-at-boundaries`.  The one
  ◆ boundary is the sub-track end; halting there gives you the completion review before the ledger
  closes.
