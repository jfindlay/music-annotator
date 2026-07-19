<!-- juncture-tier: opus -->
<!-- sub-track: R1 (_net unified network-retrieval subpackage) — first shard of the library-completion arc -->

# PLAN — R1: unified `_net` network-retrieval subpackage

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Collapse music-annotator's two structurally-different network paths (musicbrainzngs-wrapped MB+CAA;
raw-`urllib` AcoustID) into **one general-purpose `_net` retrieval core** that owns the app's
*retrieval business policy*, not MusicBrainz specifics. The core is parameterized by caller-supplied
policy: a **structured** retryable-classifier (status codes / exception types — *never*
string-scraping `str(exc)`), a terminal action, and a logging context. Every remote fetch in the
codebase routes through it.

The north star is the **lossless principle**: a retrieval that *might* have succeeded but failed to
complete is an **error**, never a silent empty. Today AcoustID's persisted-tag path violates this
(retries-exhausted returns `""` with no log — a persisted "no AcoustID" that may mean "fetch
failed"). `_net` closes that gap at the one shared terminal choke point.

**The `_net` universal terminal rule** (user decision, 2026-07-18 — the crux of this sub-track):
the terminal action is decided by the *failure-vs-no-data discrimination*, uniformly, for **all**
`_net` actions — not per-call-site:

- **Cannot determine whether the data exists** — retries exhausted, malformed response, transport
  failure → **the retrieval function raises** (never a silent empty). The raise propagates to the
  **per-release error boundary** (`discover()` at `_discover.py:966`), which logs the error and
  proceeds to the next release — a single release's retrieval failure is never fatal to the run.
  Applies to MB, CAA, *and* AcoustID (both persisted and diagnostic paths).
- **Remote server authoritatively answers "no data"** — working server returning empty results, or
  a 4xx "unknown MBID / bad fingerprint" → **warning + return empty** (legitimate no-data; a
  *success* return gated on the classifier confirming the server answered).

This is why R1 sequences first in Act I's dev work: R3 adapters (Discogs, whipper/AccurateRip)
build on `_net` from day one; the interface it freezes is consumed by every adapter.

## Verify gate

Discovered from `pyproject.toml` (`/plan-run` re-discovers; stated here to document the gate):

- **VERIFY_TEST**: `~/.local/bin/tox -e test` → `pytest tests/` with `--cov=music_annotator
  --cov-report=term-missing`; **100% branch coverage enforced** (`branch = true`, `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` → `mypy src/ tests/` (strict, `python_version =
  3.12`).
- Full gate before any row is `done`: `~/.local/bin/tox -m analyze` (build · test · check_type ·
  check_format · check_lint 10.00/10 · check_upgrade). No `Any`, no `cast()` in source.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 | `_net` core: policy-parameterized retry/backoff with structured classifier | A | Opus `@architect` | — (freezes C-NET-CORE, C-NET-TERM) | `src/music_annotator/_net.py` (new), `tests/unit/test_net.py` (new) |
| 2 | Migrate MB data calls onto `_net`; kill `str(exc)` scrape; shrink `_patched_safe_read` to MB-only | A | Sonnet | C-NET-CORE, C-NET-TERM | `src/music_annotator/_mb_api.py`, `tests/unit/test_mb_helpers.py` |
| 3 | Move CAA off musicbrainzngs onto `_net` (reimplement URL templates) | A | Sonnet | C-NET-CORE, C-NET-TERM, C-CAA-URL | `src/music_annotator/_mb_api.py`, `tests/unit/test_mb_helpers.py` |
| 4 ◆ | Migrate AcoustID onto `_net`; collapse `fetch_acoustid_lookup`; apply universal terminal rule | A | Sonnet | C-NET-CORE, C-NET-TERM | `src/music_annotator/_mb_api.py`, `src/music_annotator/_pipeline.py`, `src/music_annotator/_discover.py`, `tests/unit/test_mb_helpers.py` |

`Cat`: A = substrate. `◆` = sub-track-final row. `@architect` = inflection point (interface design
juncture — the core's contract is designed here, and `/plan-run` pages `@plan-juncture` before it
implements).

## Session detail

### S1 — `_net` core *(substrate freeze — @architect juncture)*

**Deliverable.** A new `_net.py` module exposing one retry/backoff engine parameterized by policy.
The interface (frozen here — see C-NET-CORE): a `retrieve(...)` primitive (or equivalent) taking a
zero-arg fetch callable plus a **policy** object carrying: (a) a **structured retryable-classifier**
`Callable[[Exception], RetryDecision]` reading typed status codes / exception types — never
`str(exc)`; (b) a **terminal action** encoding the universal rule (see C-NET-TERM); (c) logging
context (event name, rich fields). The 1 req/s polite-delay wrapper (`_mb_call`'s generic body) folds
in or sits beside the core. **Over-specify** per the substrate-row rule: carry a `max_attempts`
override and a `backoff_base` parameter even if MB's current 6/`2**n` is the only caller today —
adapters will want to tune these.

**KAT (≥1 required).** `test_net.py`: (1) a transient classification (503/500/OSError-shaped) retries
then raises after exhaustion — *cannot-determine → fatal*; (2) an authoritative-no-data
classification (4xx unknown / empty result) returns the empty sentinel with a warning, no raise; (3)
a permanent non-retryable, non-no-data error raises immediately; (4) the terminal choke point emits
exactly one `log.error` on fatal exhaustion. Each is a KAT because it pins the C-NET-TERM contract
behaviorally.

**Subtleties.** The classifier must express three outcomes, not two: `RETRY`, `NO_DATA` (→ warning +
empty), `FATAL` (→ error + raise). The naïve two-way retryable/permanent split is insufficient — a
4xx can be *either* no-data (unknown MBID) *or* fatal (malformed request); the policy decides. This
three-way shape is the load-bearing part of the interface and the reason S1 is `@architect`.

**Deferrals.** No consumer is migrated in S1 — the core lands with its own tests only. Migrations are
S2–S4.

### S2 — MB-data migration

**Deliverable.** Route `_get_release_by_id`, `_get_recording_by_id`, `_get_work_by_id` through the
`_net` core. Replace the `"503" in str(exc)` scrape in `_mb_retry` (`_mb_api.py:244`) with a
structured classifier reading `exc.cause.code` (`ResponseError` stores the original `HTTPError` as
`.cause`). Delete `_mb_retry`/`_mb_call` once callers move (or reduce to thin `_net` adapters).
`_patched_safe_read` stays but its surface is now **MB-data only** (CAA leaves in S3) — note this in
its docstring; do not delete yet.

**KAT.** The existing `TestMbRetry` / `TestFetchRelease` / `TestFetchRecordingDetail` /
`TestFetchWorkDetail` classes in `test_mb_helpers.py` (transient-retry, exhaustion-raise,
non-retryable-raise) must pass against the `_net`-backed path with the string-scrape gone. The
retry/exhaustion behavior is the frozen contract; the classifier internals change under it.

**Subtleties.** `_WORK_CACHE` (L1) and the recording/work L2 disk caches are orthogonal to retrieval
and must be preserved byte-for-byte. Patch targets in tests currently bind `mb.get_*` on the
`_mb_api` module — the `_net` indirection must not break where those names resolve (see Notes for
executors).

### S3 — CAA off musicbrainzngs

**Deliverable.** `fetch_cover_art` and `_fetch_rg_image` stop calling `mb.get_image_list` /
`mb.get_image` / `mb.get_release_group_image_front`; instead build the two canonical CAA URL templates
(release-level from the listing's `"image"` key; RG fallback
`https://coverartarchive.org/release-group/{id}/front`) and fetch through `_net`. Kill the `"404" in
str(exc)` / `"307" in str(exc)` scrapes at `_mb_api.py:661,701,525` — 404 is now the classifier's
**NO_DATA** outcome (image deleted after listing / no release-level art → warning + skip/fallback),
per the AGENTS.md CAA carve-out. `_patched_safe_read` no longer routes CAA; confirm its remaining
callers are MB-data only.

**KAT.** `TestFetchCoverArt*` (JPEG/PNG/MIME inference, multi-type single-fetch, 404-skip,
RG-fallback dual-fetch, cache miss/hit) must pass with CAA fetched via `_net`. The 404→skip and
404-listing→RG-fallback behaviors are the frozen contract (C-CAA-URL + the CAA carve-out).

**Subtleties.** Verify the CAA image decode against a real CAA response shape when off musicbrainzngs
(content-type quirk at `caa.py:81-84`; the app infers MIME from bytes via `_infer_mime`, so likely a
non-issue — **confirm at execution**). The canonical release-level image URLs come from the listing
JSON's `"image"` field, *not* reconstructed from coverid — preserve that.

### S4 ◆ — AcoustID migration + collapse + universal terminal rule

**Deliverable.** Route `fetch_acoustid_id` and `_fetch_acoustid_lookup_raw` through `_net`. Apply the
**universal terminal rule**: retries-exhausted / malformed / transport-failure now **raises** on
*both* the persisted path (`fetch_acoustid_id`) and the diagnostic path
(`_fetch_acoustid_lookup_raw`) — replacing today's silent `""` / `([], "")` returns. Authoritative
4xx "unknown MBID / bad fingerprint" and genuine empty results remain **warning + empty**. Collapse
`fetch_acoustid_lookup` into `_fetch_acoustid_lookup_raw` (callers slice the tuple); give the lookup
path the same disk-cache posture `fetch_acoustid_id` has. Update callers in `_pipeline.py:1424`,
`_discover.py:704`, `_pipeline_maint.py:43` to the collapsed signature and the new raising contract.

**KAT.** `TestFetchAcoustidId*` / `TestFetchAcoustidLookup` rewritten: (1) 5xx-exhausted now **raises**
(was `""`); (2) OSError-exhausted now **raises**; (3) malformed JSON now **raises** (was `""` — this
is *cannot-determine*, per the rule); (4) authoritative 4xx returns empty + warning (unchanged
outcome, now via classifier); (5) genuine empty result returns empty. These pin C-NET-TERM applied to
AcoustID.

**Subtleties.** This is the **behavior-changing** session — see Discoveries & risks D-1: the
diagnostic path's prior "never raises" contract is *overridden* by the universal rule. `discover()`'s
error handler (`_discover.py:966`) already catches and continues per release, so a raising diagnostic
path degrades to "this release skipped, logged, next release" rather than a crash — confirm that path
covers the candidate-seed call site too. On persisted-path failure the retrieval function raises
(no partial `acoustid_id` tag is written); the per-release boundary logs and moves on, exactly as
MB/CAA already behave — the release is abandoned, the run continues.

**Deferrals.** The musicbrainzngs2 migration sequencing (mbngs2-1 / `_patched_safe_read` upstreaming)
stays in BACKLOG — R1 shrinks `_patched_safe_read`'s surface but does not remove it. Post-R3
structural-audit trigger (module-boundary review of `_net` + adapters) stays in BACKLOG.

## Cross-session contracts

### C-NET-CORE — the `_net` retrieval interface *(compiler-enforced)*
- **Defined-in:** S1. **Consumed-by:** S2, S3, S4.
- The `retrieve` primitive signature + the policy object (classifier callable type, terminal-action
  encoding, logging context). *To be frozen at S1* — the `@architect` juncture writes the resolved
  signature into this subsection at execution time. Over-specified: `max_attempts` / `backoff_base`
  present even though only MB tunes them today.

### C-NET-TERM — the universal terminal rule *(test-enforced + prose)*
- **Defined-in:** S1 (as the classifier's three-outcome contract). **Consumed-by:** S2, S3, S4.
- `cannot-determine → raise` (fatal); `server-authoritative-no-data → warning + empty`. Uniform
  across MB, CAA, AcoustID. The one shared terminal choke point logs exactly one `log.error` per
  fatal exhaustion. *To be frozen at S1.* KAT-pinned in `test_net.py`.

### C-CAA-URL — canonical CAA URL templates *(prose-enforced)*
- **Defined-in:** S3. **Consumed-by:** S3 only (and any future adapter that fetches art).
- Release-level image URLs come from the listing JSON `"image"` field; RG-fallback is
  `https://coverartarchive.org/release-group/{id}/front`. 404 is NO_DATA (skip / fallback), per the
  AGENTS.md CAA carve-out. *To be frozen at S3.*

### Consumed (frozen upstream — invalidation is a destructive-HALT)
C-PROV / C-MOVE (move/verify/journal provenance, `docs/NOTES.md`); the defensive-download and
confirmation-provenance invariants (repo `AGENTS.md`). `_net` must preserve the two-layer
retry+polite-delay posture and the raise-on-data-integrity-failure contract those invariants mandate.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | `_net` core | pending | — | C-NET-CORE, C-NET-TERM |
| 2 | MB-data migration | pending | — | — |
| 3 | CAA off musicbrainzngs | pending | — | C-CAA-URL |
| 4 | AcoustID migration + collapse | pending | — | — |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **D-1 (behavior-change tension — carry into S1/S4 adjudication).** The user's universal terminal
  rule (2026-07-18) *overrides* the BACKLOG's earlier framing that the AcoustID **diagnostic** path
  keeps its "never raises" contract. Under the universal rule, a *cannot-determine* failure on the
  diagnostic path now **raises**. This is intentional and correct (failure = fatal, uniformly), but
  it is a behavior change the original `_net` write-up did not anticipate. `/plan-run`: treat any
  push-back here as **internal-continue** (the rule is user-authoritative), not additive-reshard —
  unless it surfaces a caller that genuinely cannot tolerate a raise (then adjudicate). Confirm
  `discover()`'s per-release catch covers the candidate-seed call site (`_discover.py:704`).
- **R-1 (CAA decode risk — S3).** CAA content-type quirk (`caa.py:81-84`) when leaving
  musicbrainzngs; likely a non-issue because the app infers MIME from bytes (`_infer_mime`) — but
  **verify against a real CAA response** at S3 execution before declaring the row done.
- **R-2 (mbngs2 sequencing).** `_patched_safe_read` shrinks to MB-data-only across S2/S3 but is not
  removed; its eventual removal is gated on the musicbrainzngs2 migration (BACKLOG mbngs2-1). Do not
  delete it in this sub-track.

## Notes for executors

- **Tier routing.** S1 is Opus (`@architect` juncture — the three-outcome classifier interface is the
  load-bearing design of the whole sub-track). S2–S4 are Sonnet (`@build`) — mechanical migrations
  against a frozen contract. `juncture-tier: opus` for all junctures (roadmap-fixed, user 2026-07-18:
  provenance/path-policy criticality keeps correctness high; the strong inner loop licenses small
  commits but does *not* license opting the juncture tier down).
- **Register: PEDAGOGY** off (production code, existing house style — thin mechanical docstrings per
  AGENTS.md; exposition only for the intricate three-outcome classifier).
- **Invariants to preserve.** No `Any`, no `cast()` in source. 100% branch coverage — every
  classifier outcome, exhaustion branch, and `match/case` arm needs an explicit test (add `case _: #
  pragma: no cover` on exhaustive unions). `from __future__ import annotations` in `_net.py`. `__all__`
  in `__init__.py` updated if any public name changes (AcoustID collapse in S4).
- **Test patch-target discipline.** Tests patch names where they are *bound*, not where they
  originate. The `_net` indirection must not silently relocate `mb.get_*` / `urlopen` binding sites in
  a way that breaks existing patch targets (`music_annotator._mb_api.mb.get_*`,
  `music_annotator._mb_api.urllib.request.urlopen`). If a target must move, update all patch sites in
  the same session.
- **Suggested first `/plan-run` invocation** (unproven shard pattern → halt at each boundary for
  review): `/plan-run halt-at-boundaries`. The `@architect` juncture at S1 will page `@plan-juncture`
  (opus) to design the C-NET-CORE / C-NET-TERM interface before implementation.
