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
- **FROZEN at S1 (@plan-juncture, 2026-07-18).** The resolved interface below is implementable by a
  Sonnet `@build` agent without further design decisions. All names live in `src/music_annotator/_net.py`
  with `from __future__ import annotations` at the top.

**Module preamble.**
```python
from __future__ import annotations

import enum
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_T = TypeVar("_T")
```

**(1) `RetryDecision` — the three-outcome enum.** (Full prose contract in C-NET-TERM.)
```python
class RetryDecision(enum.Enum):
    """Classifier verdict for one failed fetch attempt — the load-bearing three-outcome shape."""

    RETRY = enum.auto()    # transient; back off and re-attempt (exhaustion → FATAL terminal)
    NO_DATA = enum.auto()  # server authoritatively answered "no data" → warning + return None
    FATAL = enum.auto()    # permanent, non-no-data failure → error + raise immediately
```

**(2) The classifier callable type.** Reads *typed* attributes of the exception — status codes /
exception types — **never** `str(exc)`.
```python
type RetryableClassifier = Callable[[Exception], RetryDecision]
```
- **Ordering rule (load-bearing, KAT-pinned).** `urllib.error.HTTPError` is a subclass of `URLError`
  which is a subclass of `OSError`; `mb.ResponseError` wraps the original `HTTPError` on its `.cause`
  attribute. A classifier that tests `isinstance(exc, OSError)` before extracting an HTTP status code
  will misclassify a 4xx as a transient transport failure. **Every classifier MUST extract the typed
  status code (via `exc.code` for `HTTPError`, or `exc.cause.code` for `mb.ResponseError`) before any
  broad `OSError`/transport check.** Classifiers are supplied by the consuming session (S2/S3/S4); the
  core never inspects exception structure itself.

**(3) `NetPolicy` — the policy object.** Frozen dataclass; carries classifier, logging context, and
the over-specified backoff knobs.
```python
@dataclass(frozen=True)
class NetPolicy:
    """Caller-supplied retrieval policy: classifier + logging context + backoff knobs.

    :ivar classify: Structured retryable-classifier mapping a raised exception to a RetryDecision.
        Must read typed status codes / exception types, never str(exc). See the ordering rule.
    :ivar event: structlog event name for this retrieval class (e.g. "mb_retrieve", "caa_retrieve",
        "acoustid_retrieve"); used as the base event for warning (NO_DATA) and error (FATAL) logs.
    :ivar log_fields: Static rich fields merged into every log line for this retrieval (e.g.
        {"release_id": ...}); the terminal choke point adds attempt/wait/outcome fields.
    :ivar max_attempts: Maximum fetch attempts before exhaustion → FATAL terminal. Over-specified per
        the substrate-row rule; MB's historical value is 6.
    :ivar backoff_base: Base of the exponential back-off; sleep before attempt n is backoff_base ** n
        seconds. Over-specified; MB's historical value is 2.
    :ivar polite_delay_s: Seconds to sleep after a successful fetch to honour the 1 req/s posture
        (folds the generic body of the old _mb_call). Defaults to 1.0.
    """

    classify: RetryableClassifier
    event: str
    log_fields: dict[str, object] = field(default_factory=dict)
    max_attempts: int = 6
    backoff_base: float = 2.0
    polite_delay_s: float = 1.0
```

**(4) `retrieve` — the primitive.** One retry/backoff/terminal engine, generic in the success type.
```python
def retrieve(fetch: Callable[[], _T], policy: NetPolicy) -> _T | None:
    """Execute a zero-arg fetch under the policy's retry/backoff and universal terminal rule.

    Loops up to policy.max_attempts. Each raised exception is handed to policy.classify:
      * RETRY   → sleep policy.backoff_base ** attempt seconds, re-attempt; exhausting all attempts
                  is treated as the FATAL terminal (cannot-determine → raise).
      * NO_DATA → emit exactly one warning log ("<event>_no_data"), return None (no raise).
      * FATAL   → emit exactly one error log ("<event>_fatal"), re-raise the original exception.
    On success, sleep policy.polite_delay_s (the folded polite delay) and return the fetch value.

    :param fetch: Zero-arg callable performing exactly one network request; may raise.
    :param policy: The NetPolicy governing classification, logging, backoff, and polite delay.
    :returns: The fetch's value on success, or None when the server authoritatively answered no-data.
    :raises Exception: Re-raises the original exception on a FATAL classification or on RETRY
        exhaustion (the cannot-determine terminal). The raise propagates to the per-release error
        boundary in discover().
    """
```
- **Return-type decision (pivotal — the `None` sentinel).** `retrieve` returns `T | None`. The core
  is generic in `T` and therefore cannot know what "empty" means for an arbitrary success type; `None`
  is the **one universal NO_DATA sentinel**. Each consuming call site maps `None` to its own empty
  shape at the boundary: MB-data callers → validate `{}` / empty model; CAA callers → skip image /
  RG-fallback (`None` ≡ today's `bytes | None` None); AcoustID callers → `""` (persisted) or `([], "")`
  (diagnostic). NO_DATA is **not** encoded as "empty `T`" and **not** as a caller-supplied sentinel —
  both were rejected because they conflate authoritative-no-data with an empty-shaped success and would
  leak `T`-structure knowledge into the core. Tradeoff: a caller wanting to know *which* classifier
  rule produced the `None` does not get it in the return value — it is carried in the warning log only.
  No current call site needs that programmatically.
- **Polite-delay integration: folded in.** The 1 req/s delay is `policy.polite_delay_s`, slept inside
  `retrieve` on the success path only (not on RETRY backoff sleeps, which are separate — mirroring the
  old `_mb_call` / `_mb_retry` separation). The standalone `_mb_call` helper is retired as callers
  migrate (S2–S4); `retrieve` subsumes its generic body.
- **Exhaustion is the FATAL terminal.** RETRY exhaustion and an explicit FATAL classification share the
  single error-log choke point and both raise. On exhaustion, `retrieve` re-raises the **last** caught
  exception (preserving the transport cause) rather than synthesising a bare `RuntimeError`; this keeps
  the `.cause` chain inspectable at the per-release boundary. (S2's existing `RuntimeError("... after
  retries")`-matching tests may assert on the re-raised type; the S2 executor reconciles the match
  string against the frozen behaviour — the *raise-on-exhaustion* contract is frozen, the exact
  exception type is the executor's to align with the surviving tests.)
- **No `Any`, no `cast()`.** `_T` is a `TypeVar`; the classifier is fully typed. 100% branch coverage:
  every `RetryDecision` arm, the exhaustion branch, and any `match/case` on `RetryDecision` needs an
  explicit test (`case _: # pragma: no cover` on the exhaustive enum match).

### C-NET-TERM — the universal terminal rule *(test-enforced + prose)*
- **Defined-in:** S1 (as the classifier's three-outcome contract). **Consumed-by:** S2, S3, S4.
- **FROZEN at S1 (@plan-juncture, 2026-07-18).** The three `RetryDecision` outcomes (enum in
  C-NET-CORE) encode the universal terminal rule; `retrieve` is the single shared choke point that
  actions them uniformly across MB, CAA, and AcoustID:

| Classifier verdict | Meaning | `retrieve` terminal action | Log |
|---|---|---|---|
| `RETRY` | transient (503/500/OSError-shaped, redirect-loop) | back off `backoff_base ** attempt`, re-attempt; **exhaustion → FATAL** | (per-attempt warning optional; terminal error on exhaustion) |
| `NO_DATA` | server authoritatively answered "no data" (4xx unknown MBID / bad fingerprint / empty result set) | **return `None`** (no raise) | exactly one `log.warning("<event>_no_data", **log_fields)` |
| `FATAL` | permanent, non-no-data failure (malformed request, unclassifiable transport failure) | **re-raise** the original exception | exactly one `log.error("<event>_fatal", **log_fields)` |

- **Cannot-determine → raise.** RETRY exhaustion, a malformed/unparseable response, and any transport
  failure the classifier cannot resolve to NO_DATA all terminate as FATAL: exactly one `log.error`,
  then raise. This is the lossless principle — a retrieval that *might* have succeeded but did not
  complete is an error, never a silent empty. The raise propagates to the per-release boundary
  (`discover()`, `_discover.py:966`), which logs and proceeds to the next release.
- **Server-authoritative-no-data → warning + None.** Only when the classifier confirms the server
  itself answered (a working server returning an empty result set, or a 4xx that authoritatively means
  "this MBID/fingerprint has no data") does `retrieve` return the `None` sentinel with a single
  warning. Callers map `None` to their own empty shape (see C-NET-CORE return-type decision).
- **Exactly-one-log invariant (KAT-pinned).** Per terminal event, exactly one `log.error` (FATAL /
  exhaustion) or exactly one `log.warning` (NO_DATA) is emitted at the choke point — not zero, not two.
- **KATs in `test_net.py` (≥4, each pins this contract behaviorally):**
  1. Transient classification (503/500/OSError-shaped) → retries `max_attempts` times, then raises
     (cannot-determine → fatal).
  2. Authoritative-no-data classification (4xx-unknown / empty result) → returns `None` with exactly
     one warning, no raise.
  3. Permanent non-retryable, non-no-data classification → raises immediately (no retry loop).
  4. The terminal choke point emits **exactly one** `log.error` on fatal exhaustion.
- **D-1 reconciliation (behavior change is in-scope).** Under this uniform rule the AcoustID
  *diagnostic* path's prior "never raises" contract is overridden at S4: a cannot-determine failure now
  raises. This is user-authoritative (2026-07-18) and reconciled here — `discover()`'s per-release
  catch degrades it to "release skipped, logged, next release," not a crash. S4 confirms the catch
  covers the candidate-seed call site (`_discover.py:704`).

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
| 1 | `_net` core | done | 011668e | C-NET-CORE, C-NET-TERM |
| 2 | MB-data migration | done | 4deb288 | — |
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
