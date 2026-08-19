<!-- juncture-tier: opus -->
<!-- sub-track: pre-R6d unify-invocation fix — library-completion arc (docs/ROADMAP.md), Act III-a.
     A discovered latent defect blocking R6d's destructive unify pass: the standalone `unify`
     subcommand cannot be invoked because it lacks the `--user-agent-app`/`--user-agent-email`
     plumbing + the `init_mb()` call that `apply`/`search`/`preflight` carry, and dies with a
     musicbrainzngs.UsageError on the first artist.  Root cause: C-CANON (canonical name-forms)
     added a fetch_artist_aliases(mbid) call inside build_dest_path, so unify/repath/regroup now
     dereference each embedded artist MBID for its stable primary canonical alias.  Operator ruling
     (2026-08-19): this narrow, fixed-MBID, cached name-form dereference is a permitted determinate
     lookup, NOT a forbidden MB(*) wildcard (search / re-identification).  So the fix is plumbing +
     a docstring correction that pins the determinate-transition invariant — NOT an offline rewrite
     and NOT a C-CANON re-freeze.  One session, Sonnet, no contract freeze.  Analogous to the
     pre-R3 hardening node: a discovered pre-condition fix that lands before the gated pass. -->

# PLAN — pre-R6d fix: enable `unify` invocation + pin the determinate-transition invariant

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

R6d's destructive "more-like-itself" one-pass is `unify`-dominated (preflight evidence 2026-08-14:
9,009 `unify` moves, 0 for every other pass).  But the standalone `unify` subcommand **cannot be
run**: it has no `--user-agent-email` flag and never calls `init_mb()`, so its
`build_dest_path` → `_canonical_name` → `fetch_artist_aliases(mbid)` call — added by C-CANON to
render canonical entity name-forms — raises `musicbrainzngs.UsageError` on the first artist with an
MBID.  `apply`/`search`/`preflight` all carry this plumbing; `unify` was left behind, and its epilog
still falsely asserts "No MusicBrainz network calls are made."

**The determinate-transition principle (operator ruling 2026-08-19).**  `unify` must effect a
determinate transition A → A′, fully specified by what `unify` defines over the *current* library
state.  What is forbidden is folding in `MB(*)` — a wildcard of new/volatile MB data whose answer
drifts with MB's catalog (release search, re-identification, relationship refetch).  A fixed-MBID
dereference of an entity's own stable, primary-flagged canonical name-form is **not** a wildcard: it
is a narrow, well-defined, two-layer-cached lookup of stable data that simply isn't local yet.  So
the correct fix is to **enable the lookup** (wire the user-agent plumbing) and **document the
invariant** (correct the false epilog to state the A → A′ posture), not to make `unify` offline and
not to re-open C-CANON.

**This sub-track delivers the plumbing fix + the epilog correction, in one session.**  No new pass
logic, no model change, no contract freeze — it *reaffirms* C-CANON's network posture and pins the
determinate-transition invariant in durable prose.

**The structural facts that shape this shard (survey 2026-08-19).**

- **`unify`'s only network dependency is `_canonical_name`** (`_tags.py:1263`): for any performer
  `ArtistEntry` with an MBID, it calls `canonical_artist_form(fetch_artist_aliases(entry.mbid))`.
  MBID-less entries return `entry.name` with no call.
- **`fetch_artist_aliases` is two-layer cached** (`_mb_api.py:1047` L1 in-process, `:1053` L2
  on-disk JSON) — live traffic is bounded by *distinct artist MBIDs*, not file count, and is
  effectively local after warm-up.
- **The plumbing pattern is established three times** — `apply` (`__main__.py:957`), `search`
  (`:980`), and `preflight` (`:1120`) all assemble `f"{args.user_agent_app} {args.user_agent_email}"`
  and pass it to `init_mb()` / `run()`.  `preflight`'s `_run_preflight` (`:1106`) is the exact model:
  it calls `init_mb(...)` *because the unify pass needs it*.
- **`unify_parser` (`:750`) has only `dest_dir` / `--dry-run` / `--yes`;** the `case "unify":`
  dispatch (`:1071`) calls `music_annotator.unify(...)` with no `init_mb()`.
- **The `unify` epilog (`:739`) falsely asserts "No MusicBrainz network calls are made"** — true
  before C-CANON, false now.
- **There is currently no `unify` dispatch test in `test_main.py`** (only a docstring mention at
  `:1768`).  The new `init_mb` branch must be covered; verify the pre-existing `case "unify":` arm's
  coverage source so the added call does not open an uncovered branch.

## Verify gate

Discovered from `pyproject.toml` (tox envs; do not assume `make`).  Binding — this is a code shard.

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage**, `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict).
- Full gate before ledger-done: `~/.local/bin/tox -m analyze` (build + test + check_type +
  check_format + check_lint 10.00/10 + check_upgrade).  Import order via `~/.local/bin/tox -m edit`,
  never hand-edited.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 ◆ | Wire `unify`'s MusicBrainz user-agent so the canonical name-form lookup can run | I | Sonnet | C-CANON (validate-only), the `apply`/`search`/`preflight` user-agent-plumbing convention | `src/music_annotator/__main__.py`, `tests/unit/test_main.py`, `docs/NOTES.md` |

`Cat`: **I (integrative)** — the CLI is where `unify`'s public form is completed; the fix gives the
already-built pass its operator-visible invocation surface and pins the determinate-transition
invariant in durable prose.  Single-session sub-track, so the one row is the ◆ boundary.

`Tier`: **Sonnet.**  Mechanical over three established precedents (`apply`/`search`/`preflight`
plumbing), no contract freeze, strong inner loop (100% branch + strict mypy).  Lever 3/4 (design-error
cost / correctness-crit) is low: the posture decision is already made (operator ruling); this only
enacts it.  `juncture-tier: opus` — kept (arc default), but no juncture fires in a one-row shard.

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.

- **S1 ≈ 40–90 LOC, 3 files** (two `add_argument` calls + the `init_mb` dispatch line + the epilog
  rewrite + the NOTES invariant prose + the CLI tests).  **Under band by design** — the change is a
  plumbing-symmetry fix, not a build.  **Irreducible unit (lever 2, floor):** the flags, the
  `init_mb` call, and the epilog correction are one conceptual unit ("make `unify` invocable and
  honestly documented") — splitting them would leave a half-wired subcommand or a still-false epilog.
  Kept whole.  One-line commit-title passes.

## Session detail

### S1 ◆ — Wire `unify`'s MusicBrainz user-agent so the canonical name-form lookup can run

**Deliverable.**  Enable and honestly document `unify` invocation:
- **Add `--user-agent-app` + `--user-agent-email` to `unify_parser`** (`__main__.py:750`), mirroring
  the `preflight_parser` definitions (`:875`–`:890`) verbatim: `--user-agent-app` defaults to
  `_DEFAULT_USER_AGENT_APP`; `--user-agent-email` defaults to `""`, help text stating it is required
  when the library contains files with `MUSICBRAINZ_ARTISTID` tags because `unify` calls
  `fetch_artist_aliases` for canonical name-forms.
- **Call `init_mb()` in the `case "unify":` dispatch** (`:1071`), assembling
  `f"{args.user_agent_app} {args.user_agent_email}".strip()` exactly as `preflight`'s
  `_run_preflight` does (`:1120`), before `music_annotator.unify(...)`.
- **Correct the false epilog** (`:739`).  Replace "The join key is the embedded MUSICBRAINZ_ALBUMID
  tag, not the journal (C-W2).  No MusicBrainz network calls are made." with prose that states the
  true posture: `unify` reads the embedded `MUSICBRAINZ_ALBUMID` join key and effects a determinate
  re-layout of the *current* library state; it dereferences each embedded artist MBID for its stable,
  primary-flagged canonical name-form (a narrow, cached, fixed-MBID lookup — never a MusicBrainz
  *search* or re-identification), so it makes no wildcard MB call and requires the user-agent.
- **Pin the determinate-transition invariant in `docs/NOTES.md`** as a prose contract (alongside the
  existing "path is a handle, not a manifest" / "journal detects, tag adjudicates" prose contracts):
  offline maintenance passes effect a determinate A → A′ over current library state and may perform
  narrow, stable, fixed-MBID MB dereferences (canonical name-forms), but never a wildcard `MB(*)`
  call (search / re-identification / relationship refetch) — those are separate, deliberate actions.

**KAT (the row's behavioural witnesses).**  In `test_main.py`, modelled on the `preflight`
init_mb/arg tests (`:1714`–`:1815`):
(a) **arg-parse witnesses** — `unify --user-agent-email t@x.com` stores `user_agent_email == "t@x.com"`;
`--user-agent-app MyApp/2.0` stores it; the app default contains `_VERSION`; email defaults to `""`
(mirrors `test_preflight_user_agent_*`).
(b) **init_mb-called witness** — a `unify` dispatch test (patching `music_annotator.init_mb` and
`music_annotator.unify`, argv-driven, per `test_preflight_dispatches_*`) asserts `init_mb` is called
once with the assembled `"{app} {email}"` string **before** `unify` is invoked, and that the
trailing space is stripped when email is empty (mirrors
`test_preflight_init_mb_called_with_default_user_agent`).
(c) **dispatch-forwarding witness** — the same test asserts `unify` receives `dest_root` / `yes` /
`dry_run` forwarded correctly (this is also the first explicit `case "unify":` dispatch test — it
closes the pre-existing coverage gap).

**Subtleties.**
- **No offline rewrite, no C-CANON change.**  `_canonical_name` / `build_dest_path` /
  `fetch_artist_aliases` are untouched.  The fix is CLI plumbing + docstring + NOTES prose only.
- **Coverage of the new `init_mb` line.**  There is no existing `unify` dispatch test; confirm the
  `case "unify":` arm's current coverage source and ensure the added `init_mb` call is covered by KAT
  (b)/(c) so branch coverage stays 100%.
- **Register discipline.**  The epilog and NOTES prose state the *property/invariant*
  (determinate A → A′; stable fixed-MBID lookup vs wildcard `MB(*)`), never a plan coordinate — no
  "pre-R6d", no "S1", no "R6d".

**Deferrals.**  No strict-determinism tag persistence (the residual reopen trigger — persist the
canonical form at ingest, read offline — is deferred; only fires if an MB primary-alias drift
between ingest and `unify` is observed to re-path a dir).  No `repath`/`regroup` change (they share
the same posture but were not the invocation defect; the NOTES invariant covers them by property).

**◆ boundary (register anneal).**  Re-read Purpose.  Confirm the row enacted, `tox -m analyze` green,
ledger complete.  **Planning-register anneal:**
- Durable files (`__main__.py` epilog/dispatch, `test_main.py`, `NOTES.md`) carry **no plan
  coordinates** — state the property/invariant (the determinate-transition posture), never
  "S1"/"pre-R6d"/"R6d".
- Grep durable files against the anneal denylist (Notes for executors); translate any leaked
  coordinate to standalone prose.
- Report to the roadmap: `unify` is now invocable; the determinate-transition invariant is pinned in
  NOTES; C-CANON's network posture reaffirmed (refined R6a property: no *wildcard* MB call, stable
  fixed-MBID dereference permitted).  R6d's destructive `unify` run can now supply the user-agent.

## Cross-session contracts

*(No contract frozen this sub-track.)*

### Consumed (frozen upstream — validate-only)

- **C-CANON** — canonical entity name-forms via `canonical_artist_form` over MB primary-flagged
  aliases (STYLEGUIDE 3.1/NORM-2).  This shard **enables** the lookup C-CANON requires and reaffirms
  its network posture; it does not modify or re-freeze it.
- **The `apply`/`search`/`preflight` user-agent-plumbing convention** — `f"{app} {email}".strip()`
  → `init_mb()`.  `unify` is made symmetric with it.  Validate-only.
- **The determinate-transition prose invariant** (pinned in NOTES this shard) — offline maintenance
  effects A → A′ and may perform stable fixed-MBID MB dereferences, never a wildcard `MB(*)`.
- **C-PROV / C-MOVE + confirmation-provenance** — untouched; `unify`'s mutating path is not modified.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 ◆ | Wire `unify`'s MusicBrainz user-agent so the canonical name-form lookup can run | pending | | (no contract; pins determinate-transition invariant in NOTES) |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **D-1 (the invocation defect — this shard's reason).**  Standalone `unify` cannot run: no
  `--user-agent-email` flag, no `init_mb()` in dispatch, so C-CANON's `fetch_artist_aliases` call
  raises `UsageError`.  Resolution: wire the plumbing (mirrors `apply`/`search`/`preflight`).
  *internal-continue.*
- **D-2 (posture ruling — the name-form lookup is not `MB(*)`).**  Operator ruling 2026-08-19: a
  fixed-MBID dereference of a stable primary canonical name-form is a permitted determinate lookup,
  not a forbidden wildcard.  So the fix enables + documents, never makes `unify` offline.  Refines
  the R6a "offline passes make no MB call" property to "no *wildcard* MB call."  *internal-continue.*
- **D-3 (strict-determinism residual — reopen trigger, deferred).**  If an MB primary alias drifts
  between ingest and a `unify` run, `unify` would re-path that dir — the one place A → A′ isn't a
  pure function of local state.  Rare and arguably correct.  If strict determinism is later required,
  persist the canonical form as an embedded tag at ingest and read it offline.  Deferred; not this
  shard's scope.  *internal-continue.*
- **D-4 (pre-existing `unify` dispatch coverage gap).**  No `case "unify":` dispatch test exists,
  yet coverage is 100% — verify the current coverage source before adding `init_mb`, so the new call
  does not open an uncovered branch.  KAT (b)/(c) close the gap.  *internal-continue.*

## Notes for executors

- **Tier routing.**  S1 is **Sonnet** (mechanical CLI plumbing over three precedents; no contract
  freeze).  `juncture-tier: opus` kept (arc default); no juncture fires in a one-row shard.
- **Enable, don't rewrite.**  Do **not** make `unify` offline, add a resolver param, or change
  `build_dest_path`/`_canonical_name`/`fetch_artist_aliases`.  The fix is CLI plumbing + epilog +
  NOTES prose.
- **Mirror `preflight` exactly.**  `unify_parser`'s two new args mirror `preflight_parser:875–890`;
  the dispatch `init_mb` mirrors `_run_preflight:1120` (`.strip()` on the assembled string).
- **REGISTER rule (durable-file discipline).**  In source/tests/NOTES, state the
  *property/invariant* — the determinate A → A′ posture; stable fixed-MBID lookup vs wildcard
  `MB(*)` — never a plan coordinate.  Plan vocabulary (S1, pre-R6d, R6d, sub-track, `/plan-run`)
  lives only in `PLAN.md`/`ROADMAP*.md`/ledger/commit messages.  See the repo `AGENTS.md` "Register
  rule" block.
- **Anneal denylist (◆ gate greps durable files for these).**  Seeded from `/plan-run` default,
  tuned for this project:
  - `\bS[1-9]\b` (plan session coordinates) — **but** allow STYLEGUIDE rule-section forms
    (`\b[1-5]\.[0-9]\b` like "3.1", "4.5" are register cites, not plan coordinates — do **not** flag).
  - `\bR6[a-e]\b`, `\bR[0-9]\b`, `\bJ[1-3]\b` (roadmap node + juncture coordinates) — flag in durable
    source/tests; legitimate only in PLAN/ROADMAP/ledger/commit messages.
  - `pre-R6d`, `sub-track`, `plan-run`, `plan-shard`, `halt-at-boundaries`, `run-to-boundary` — flag
    as plan coordinates.
  - `juncture`, `inflection`, `action-frame`, `◆`
  - Do **not** add `unify`, `repath`, `regroup`, `build_dest_path`, `fetch_artist_aliases`,
    `canonical_artist_form`, `init_mb`, `user-agent`, `MUSICBRAINZ_ALBUMID`, `MUSICBRAINZ_ARTISTID`,
    `C-CANON`, `MB(*)` — these are legitimate domain/API/contract vocabulary this shard renders.
  - Contract names in docstrings (`C-CANON`) are the intended durable form; flag bare "S1 freeze"-
    style prose, not the contract name.
- **Invariants to preserve:** the determinate-transition invariant (offline maintenance = A → A′ +
  stable fixed-MBID dereference, never wildcard `MB(*)`); C-CANON's canonical name-form posture
  (validate-only); the `unify` mutating-path provenance chain (untouched).
- **Every row runs `~/.local/bin/tox -m analyze` before ledger-done.**  Import order via
  `~/.local/bin/tox -m edit`, never hand-edited.
- **Suggested first `/plan-run` invocation:** `run-to-boundary` — a single-row shard with no
  contract freeze and an already-decided posture; run the row through its ◆ in one pass.  (No
  `halt-at-boundaries` step is needed: there is no unproven substrate judgment to check mid-shard.)
