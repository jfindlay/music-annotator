<!-- juncture-tier: opus -->
<!-- sub-track: styleguide-sync follow-on — library-completion arc (docs/ROADMAP.md).  The
     C-UNIVERSAL re-freeze (prior PLAN, ledger done, commit bec261d) deleted _top_level_class,
     renamed _classical_top_dir → _top_dir_component, and decoupled IS_CLASSICAL to the work-type
     predicate.  It deferred the durable-prose sync out of the code-freeze session (D-5; the code
     gate does not block on prose accuracy).  Durable files still describe the deleted C-CLASS as a
     live frozen contract, misstating the frozen policy.  This shard pays that debt: it edits
     comments + human docs only, freezing no new contract — it re-aligns durable prose to the
     already-frozen C-UNIVERSAL / epistemic-criterion contracts. -->

# PLAN — styleguide-sync follow-on: durable prose to the C-UNIVERSAL freeze

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

The C-UNIVERSAL re-freeze changed the code (class prefix deleted, first-component rule generalised,
`IS_CLASSICAL` decoupled to the work-type predicate) but **deliberately deferred the durable-prose
sync** so the code gate would not block on documentation accuracy.  As a result, several durable
files now **misstate the frozen policy** — they describe the deleted `_top_level_class` / C-CLASS
routing as a live frozen contract:

- `src/music_annotator/models.py:1385` — the `is_classical` field comment says the value derives
  "from `_top_level_class`" (a deleted function).  **This is the most material defect**: it misstates
  the just-frozen `IS_CLASSICAL` basis (the CE-classical work-type predicate).
- `src/music_annotator/_tags.py:1156` — a comment references the renamed `_classical_top_dir`.
- `docs/STYLEGUIDE.md` 4.5 (path grammar lists "class directory;"), REND-22 (describes C-CLASS as a
  live frozen contract), REND-23 (describes C-INIT as separate), REND-21 (a caveat "if ever
  generalised … noted for the application shards" that the decouple now *satisfies*).
- `docs/census-impl.md` 5.1 / 5.2 / REND-21 / REND-22 / REND-23 — describe the deleted
  `_top_level_class` routing and stale `_tags.py` source-line refs as live.

**This shard re-aligns durable prose to the already-frozen contracts.**  It freezes **no new
contract**.  It edits **comments and human docs only** — no `.py` logic changes, no behaviour change,
no path or tag output change.  It is the register-anneal the deferral left owed: durable files must
state the current property/invariant, never a superseded one.

**The epistemic criterion and C-UNIVERSAL are already frozen** (prior shard, NOTES + code).  This
shard does not re-open them; it makes the descriptive layer honest about them.

**Out of scope (named, not silently dropped).**

- **No code logic.**  The frozen behaviour stands; only comments describing it are corrected.
- **No J3 preflight re-run.**  That is the next *operator* step (run `scripts/preflight_r6d.py`
  against live hades under the re-frozen policy).  It is not an agent code session — the harness is
  built and green (C-PREFLIGHT).  Noted here so the boundary hand-off names it.
- **No `_CLASS_VOCAB` / discriminator removal.**  That is post-R6d (the live library still holds
  3-level class-prefixed dirs until the destructive pass migrates them).

## Verify gate

Discovered from `pyproject.toml` (tox envs; do not assume `make`).

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage**).  This
  shard touches only comments in `.py` files, so coverage is unchanged by construction — the gate
  proves the comment edits did not disturb any line/branch structure.
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict) — comment-only
  `.py` edits are type-neutral; the gate confirms no accidental code disturbance.
- Full gate before ledger-done: `~/.local/bin/tox -m analyze`.  Import order via
  `~/.local/bin/tox -m edit`, never hand-edited.
- **Accuracy check (this shard's real verification, standing in for a KAT — see Session detail):**
  after the edits, **no live reference to `_top_level_class` survives in durable files** except as a
  superseded-name mention in an explicit status note.  Verified by grep, not a runtime test — this
  is a documentary shard whose "contract" is descriptive accuracy, not behaviour.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 ◆ | Sync durable prose (code comments + styleguide + impl census) to the C-UNIVERSAL freeze | I | Sonnet | C-UNIVERSAL, the epistemic criterion, REND-21 (satisfied caveat) | `src/music_annotator/models.py`, `src/music_annotator/_tags.py`, `docs/STYLEGUIDE.md`, `docs/census-impl.md` |

`Cat`: **I (integrative)** — the styleguide and impl census are where the catalog lens's policy gets
its *public descriptive form*; this row re-aligns that description to the frozen contract.  The
integrative session is "where contracts get their public form" — for a documentary sync that is the
whole deliverable.  Single-session sub-track, so the one row is the ◆ boundary.

`Tier`: **Sonnet.**  Pure documentary re-alignment against a frozen contract — no design surface, no
cost-of-wrong beyond "did we miss a reference" (the accuracy grep catches that).  Lever 3/4
(design-error / correctness-crit) is minimal: nothing executes.  `juncture-tier: opus` kept (arc
default); no juncture fires in a one-row shard.

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.

- **S1 ≈ 30–80 LOC across 4 files**, all prose (two `.py` comment blocks; ~5 STYLEGUIDE
  paragraphs/register lines; ~4 census-impl entries).  **Below the default band by design** — this is
  a documentary follow-on, not a conceptual unit with irreducible code complexity (lever 2 floor is
  small).  **Not split further**: the defect is one conceptual unit ("durable prose still names the
  deleted contract"); splitting comments from docs would fracture a single accuracy pass at a
  non-contract-sharp boundary.  Kept whole.  One-line commit-title passes.

## Session detail

### S1 ◆ — Sync durable prose to the C-UNIVERSAL freeze

**Deliverable.**  Correct every durable-file description of the deleted C-CLASS / `_top_level_class`
routing to describe the frozen C-UNIVERSAL policy.  Concretely:

- **`models.py:1385`** — rewrite the `is_classical` field comment.  Old: "build_track_tags overrides
  this explicitly from `_top_level_class` (STYLEGUIDE 4.7/REND-21) so the persisted value reflects
  the actual library class."  New: the flag derives from the **CE-classical work-type predicate**
  (`cwp_work_top` non-empty AND `"Classical" in cwp_worktype_genres_top`) — compositional identity,
  not the code path (REND-21/SEL-14; tag layer ≠ path layer).  State the *property*, no plan
  coordinate.
- **`_tags.py:1156`** — update the comment referencing `_classical_top_dir` to name
  `_top_dir_component` and describe the branch as **performer-led** (not "recital"): when
  `raw_composer` is empty the helper returns the performer-led shape, so `composer=""` is never used
  in the path.
- **`STYLEGUIDE.md 4.5`** — drop "class directory;" from the path-grammar component list; change "The
  class and top-directory routings realise the C-CLASS and C-INIT contracts" to: the first-component
  routing realises **C-UNIVERSAL** (which superseded C-CLASS; the catalog path is prefix-less).
- **`STYLEGUIDE.md REND-22`** — status → **C-CLASS refuted-and-deleted; superseded by C-UNIVERSAL**
  (prefix-less universal top dir; editorial class distinctions relocated to the playlist lens).
- **`STYLEGUIDE.md REND-23`** — status → **C-INIT absorbed and generalised into C-UNIVERSAL** (the
  first-component rule is universal; a pop album is the performer-led branch).  Keep the CE recital
  divergence note (still true: where MB links no composer, the album artist renders).
- **`STYLEGUIDE.md REND-21`** (both the register line ~:593 and the CE-divergence line ~:650) — note
  the "must derive from the classification, never the code path" caveat is now **satisfied** (the
  flag derives from the work-type predicate directly), not "noted for the application shards."
- **`census-impl.md` 5.1 / 5.2 / REND-21 / REND-22 / REND-23** — mark the `_top_level_class` routing
  and its source-line refs as **superseded** (C-CLASS deleted → C-UNIVERSAL; `_classical_top_dir` →
  `_top_dir_component`).  This is an impl-census (a point-in-time survey artifact); a light
  superseded-status stamp suffices — do not re-author the whole census.

**KAT — flagged: this row has no KAT, and here is why the contract is still defined.**  Per the
sharding rule, a row whose deliverable can't be a KAT usually has an undefined contract.  This row is
the deliberate exception: it freezes **no behavioural contract** — it is a documentary re-alignment
to a contract frozen upstream (C-UNIVERSAL, already KAT-witnessed in the prior shard's tests
(a)–(d)).  There is nothing new to witness at runtime.  **The verification that stands in for a KAT
is the accuracy grep** (Verify gate): after the edits, no durable file names `_top_level_class` as a
live routing (only as an explicit superseded-name status mention).  This is the anneal test, not a
behaviour test — appropriate for a Cat-I documentary sync.  If, in doing the work, the executor finds
any edit *does* change behaviour, that is a signal this was mis-scoped as documentary — halt and
surface (it should not happen; every edit is a comment or a `.md`).

**Subtleties.**

- **Comment-only `.py` edits must stay comment-only.**  `models.py:1385` and `_tags.py:1156` are
  inside docstrings/comments.  Editing them must not touch the adjacent code (the `is_classical: str =
  "1"` default stays; the `raw_composer` logic stays).  `tox -e test` at 100% branch coverage
  confirms no structural disturbance.
- **`census-impl.md` is a survey artifact, not a spec.**  It records what the code was at survey time.
  The honest fix is a superseded-status stamp ("C-CLASS deleted 2026-08-19; see C-UNIVERSAL"), not a
  rewrite that would falsify its point-in-time nature.  Weigh whether a census artifact should be
  touched at all versus a single dated superseded-header — the executor picks the lighter honest
  option and states which.
- **Register discipline.**  All edited prose states the *property/invariant* (the flag derives from
  compositional identity; the catalog path is prefix-less; C-UNIVERSAL superseded C-CLASS) — never a
  plan coordinate.  Contract names (C-UNIVERSAL, C-INIT, C-CLASS-as-superseded-name, REND-21/22/23,
  SEL-14) are legitimate durable vocabulary.

**Deferrals.**

- **J3 preflight re-run** (operator, live library) — the next step *after* this shard, not in it.
- **`_CLASS_VOCAB` / discriminator removal** (post-R6d) — the live library still holds 3-level dirs.
- **`census-impl.md` full re-survey** — only if a future arc needs a fresh impl census; a
  superseded-stamp is sufficient now.

**◆ boundary (register anneal).**  Re-read Purpose.  Confirm the row enacted, `tox -m analyze` green,
ledger complete.  **Planning-register anneal:**

- Durable files carry **no plan coordinates** — state the property/invariant, never "S1"/"J2"/"R6d".
- Grep durable files against the anneal denylist (Notes for executors); translate any leaked
  coordinate to standalone prose.  **This shard's own accuracy grep** (no live `_top_level_class`
  reference) is part of the same pass.
- Report to the roadmap: styleguide-sync debt **paid**; durable prose now describes C-UNIVERSAL, not
  the deleted C-CLASS.  The C-UNIVERSAL sub-track's descriptive layer is closed.

## Cross-session contracts

### Produced (frozen this sub-track)

- **None.**  This is a documentary sync; it freezes no new contract.  (A Cat-I row that freezes no
  contract is legitimate only when it is a re-alignment to an already-frozen contract — which this
  is.)

### Consumed (frozen upstream — validate-and-describe only)

- **C-UNIVERSAL** (prior shard, commit `bec261d`) — the prefix-less universal-top-dir catalog policy.
  This shard makes durable prose describe it accurately.
- **The epistemic-criterion prose contract** (NOTES, prior shard) — defer to MB where variation is
  scholarship-driven; never let free-classification parameters define topology.  The corrected
  STYLEGUIDE/census prose is consistent with it.
- **REND-21 / SEL-14** — `IS_CLASSICAL` derives from compositional identity (the work-type
  predicate).  This shard records that REND-21's caveat is now satisfied.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 ◆ | Sync durable prose (code comments + styleguide + impl census) to the C-UNIVERSAL freeze | done | 02a9c83 | (none — documentary re-alignment; no new contract) |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **D-1 (why this shard exists).**  The C-UNIVERSAL re-freeze deferred the durable-prose sync out of
  the code-freeze session (prior PLAN D-5); durable files still describe the deleted C-CLASS as live.
  Resolution: this documentary follow-on.  *internal-continue.*
- **D-2 (the one risk — a "documentary" edit that isn't).**  If any planned edit turns out to change
  behaviour (e.g. a comment that a test string-matches, or a doc that a doctest executes), the row is
  mis-scoped.  Guard: `tox -e test` at 100% branch coverage; every edit is a comment or `.md`.  If it
  fires, halt and surface — do not force a behavioural change through a documentary shard.
  *internal-continue (destructive-HALT if behaviour changes).*
- **D-3 (census-impl is a point-in-time artifact).**  Rewriting it would falsify its survey nature; a
  dated superseded-status stamp is the honest fix.  Executor picks the lighter honest option.
  *internal-continue.*

## Notes for executors

- **Tier routing.**  S1 is **Sonnet** (documentary re-alignment to a frozen contract).  `juncture-tier:
  opus` kept (arc default); no juncture fires in a one-row shard.
- **Order.**  (1) `models.py:1385` (most material — the misstated `IS_CLASSICAL` basis); (2)
  `_tags.py:1156`; (3) STYLEGUIDE 4.5 + REND-21/22/23; (4) census-impl superseded-stamps.
- **The accuracy grep is mandatory and stands in for a KAT.**  Before ledger-done, grep all durable
  files for `_top_level_class`: the only permitted survivors are explicit superseded-name status
  mentions.  Grep for "class directory" in STYLEGUIDE 4.5: must be gone from the component list.
- **REGISTER rule (durable-file discipline).**  In source/tests/docs, state the
  *property/invariant* — the flag derives from compositional identity; the catalog path is
  prefix-less; C-UNIVERSAL superseded C-CLASS — never a plan coordinate.  Plan vocabulary (S1, J2,
  J3, R6d, sub-track, `/plan-run`) lives only in `PLAN.md`/`ROADMAP*.md`/ledger/commit messages.
- **Anneal denylist (◆ gate greps durable files for these).**
  - `\bS[1-9]\b` (plan session coordinates) — **but** allow STYLEGUIDE rule-section forms
    (`\b[1-5]\.[0-9]\b` like "4.5", "4.7" are register cites — do **not** flag).
  - `\bR6[a-e]\b`, `\bR[0-9]\b`, `\bJ[1-3]\b` (roadmap node + juncture coordinates) — flag in durable
    source/docs; legitimate only in PLAN/ROADMAP/ledger/commit messages.
  - `sub-track`, `plan-run`, `plan-shard`, `halt-at-boundaries`, `run-to-boundary`, `juncture`,
    `inflection`, `action-frame`, `◆`, `naming-policy re-freeze` (as a coordinate).
  - Do **not** flag: `C-UNIVERSAL`, `C-INIT`, `C-CLASS` (as a superseded-contract *name* in a status
    note), `REND-21`/`REND-22`/`REND-23`, `SEL-14`, `IS_CLASSICAL`, `_CLASS_VOCAB`,
    `_top_level_class` (as a superseded-name mention), `_top_dir_component`, `build_dest_path`,
    `cwp_work_top`, `cwp_worktype_genres_top` — legitimate domain/contract vocabulary this shard
    renders.
- **Invariants to preserve:** C-UNIVERSAL (prefix-less scholarship-stable topology); the epistemic
  criterion; tag layer ≠ path layer (`IS_CLASSICAL` from the work-type predicate); the two-lens
  principle.  This shard touches none of them in code — only their descriptions.
- **Every row runs `~/.local/bin/tox -m analyze` before ledger-done.**  Import order via
  `~/.local/bin/tox -m edit`, never hand-edited (no import changes expected — comment/doc only).
- **Suggested first `/plan-run` invocation:** `run-to-boundary` — a single-row documentary shard;
  run it through its ◆ in one pass.  Watch item: the accuracy grep (no live `_top_level_class`
  reference) is the gate to slow down on before committing.
