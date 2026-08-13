<!-- juncture-tier: opus -->
<!-- sub-track: R6b (catalogue-colon part-label retro-fix) — library-completion arc (docs/ROADMAP.md), Act
     III-a.  Build the offline tag-content-repatch machinery that detects a bare-catalogue CWP_PART label
     (the pre-fix ": " bug output) and re-derives it offline from the embedded CWP_WORK pair via
     strip_common_prefix (the shipped forward fix), rewriting CWP_PART_* + CWP_GROUPHEADING.  CODE-ONLY:
     the destructive library-wide repatch rides R6d's one J3-gated pass (D-A5 precedent); this shard closes
     the ROADMAP R6d tag-content gap for the catalogue-colon case.  This IS a /plan-run target: the detect+
     re-derive contract + the offline repatch pass + tests, verifiable by the src/tests gate; the fresh
     NN-NN / bare-catalogue-CWP_PART scan is the S3 gating step (operator mounts the library). -->

# PLAN — R6b: catalogue-colon part-label retro-fix (offline tag-content repatch)

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

The colon-fallback in `strip_common_prefix` once split on the *first bare* `":"` to separate a
`Title: Movement` label.  MusicBrainz work titles embed a colon *inside* catalogue numbers — Haydn
Hoboken (`"…, Hob. III:31"`), Bach/Handel subtitles — so a title reaching the fallback with only a
catalogue colon produced a **bare catalogue fragment** as the part label (`CWP_PART_1 = "31"`), which
minted intermediate dirs `01 - 31`, `02 - 32`, … and the same corruption in `CWP_GROUPHEADING`
(`"String Quartets, op. 20 :: 31 :: I. Allegro moderato"`).  **The forward fix has shipped**
(`_works.py:208` keys on `": "` — colon-followed-by-space — so new ingests are correct; NORM-9,
ratified in STYLEGUIDE 4.x).  This shard is the **deferred retro-fix of releases already on disk**
(BACKLOG "Catalogue-colon part-label retro-fix").

**The structural fact that shapes this shard (survey 2026-08-12).**  R6b is **not** a paths-only
repath the way R6a was.  R6a changed only *rendering*: `build_dest_path` re-derives depth from
still-correct `CWP_PART_LEVELS` tags, so R6d's paths-only `repath` renders the fix for free.  Here the
**embedded tag content itself is corrupt**, and `build_dest_path` reads `CWP_PART_{i}` **verbatim**
(`_tags.py:1414`) — so `repath` alone re-renders `01 - 31` from the corrupt tag.  The survey confirmed
**no existing offline pass rewrites `CWP_PART_*` / `CWP_GROUPHEADING` tag content**: `repath` /
`regroup` / `unify` are paths-only (they consume those tags read-only); `enrich` writes only
fingerprint fields.  So R6b's core deliverable is **new offline tag-content-repatch machinery**.

**The enabling fact (mirrors R6a's no-MB-call resolution).**  The corrected label is re-derivable
**offline, with no MB network call**, from data already embedded in the file: `build_cwp_tags` derives
each label as `strip_common_prefix(CWP_WORK_i, CWP_WORK_{i+1})` (`_tags.py:526`), and both work-title
levels are present in the file as `CWP_WORK_{i}` tags alongside the corrupt `CWP_PART_{i}`.  Applying
the *shipped* `strip_common_prefix` to that embedded pair reproduces the correct label.  This is the
S1 inflection: the detection predicate and the offline re-derivation are the load-bearing judgment.

**Interface posture (resolved at this PLAN derivation — the S1 inflection judgments):**

1. **Re-derive offline from embedded `CWP_WORK` tags, not from MB.**  The repatch recomputes the label
   as `strip_common_prefix(CWP_WORK_i, CWP_WORK_{i+1})` over the embedded work titles — the same call
   the forward fix uses at build time — so the repatch is deterministic, offline, and network-free.
   Chosen over a re-fetch-from-MB pass because the corrected label is a pure function of data already
   in the file; a network pass would be slower, fallible, and would re-open identity questions this
   fix does not touch.  **Tradeoff:** relies on the embedded `CWP_WORK_{i}` titles being intact (they
   are — only the *derived* `CWP_PART` was corrupted, never the source `CWP_WORK`); if a file somehow
   lacks the `CWP_WORK` pair the repatch cannot recompute and must leave the tag untouched + flag it —
   worse on completeness than an MB re-fetch, accepted because the survey shows the `CWP_WORK` levels
   are always written alongside `CWP_PART` (`_tags.py:1015–1022`).
2. **Detect by re-derivation disagreement, not by a catalogue-fragment regex.**  A `CWP_PART_{i}` is
   corrupt iff it differs from `strip_common_prefix(CWP_WORK_i, CWP_WORK_{i+1})` recomputed under the
   fixed rule.  Chosen over "looks like a bare number / matches a Hob./BWV/HWV pattern" because the
   forward fix is already the authority on what the label *should* be — detecting by disagreement
   makes the predicate structural (it fires exactly where the old bug fired) and needs no
   per-composer catalogue table.  **Tradeoff:** the predicate rewrites *any* label that disagrees
   with the current rule, not only catalogue-colon cases — so a future unrelated rule change would
   make it repatch more broadly.  Accepted and bounded: scope this pass to the catalogue-colon
   signature (a disagreement where the *old* label is a prefix-free fragment of the recomputed one),
   and let R6d's one-pass own any broader re-derivation.
3. **Code-only; destructive library-wide repatch rides R6d (D-A5 precedent).**  This shard builds and
   freezes the machinery and proves it on fixtures via the src/tests gate; it does **not** run the
   repatch destructively on the live library.  R6d runs it under J3, as one part of its one-pass —
   and this machinery *closes the R6d tag-content gap* for the catalogue-colon case.  **Matches R6a /
   the canonical-name-forms precedent.**  **Tradeoff:** the ~16 latent Haydn/Bach/Handel releases stay
   corrupt on disk until R6d — the accepted D-A4/D-A6-style temporary inconsistency — worse on
   immediate library uniformity than an in-shard repatch, accepted to keep this shard off J3 and
   inside the fast src/tests inner loop.

**Sequencing (D-A5/D-A7 precedent).**  Code-only: the repatch pass is built and unit-proven; the
destructive library-wide repatch is R6d's one J3-gated pass.  No destructive library operation in R6b.

The three sessions, in landing order:

1. **S1 @architect — Detect + offline re-derivation substrate.**  Add the corruption-detection
   predicate (disagreement with the recomputed label; catalogue-colon signature) and the offline
   re-derivation helper (`strip_common_prefix` over the embedded `CWP_WORK` pair, no MB call).
   Freezes **C-CAT-INT**.
2. **S2 — Offline repatch pass (write-back + `CWP_GROUPHEADING` rebuild).**  Add the maintenance pass
   that applies the S1 re-derivation to embedded tags — rewrite `CWP_PART_*`, rebuild `CWP_GROUPHEADING`
   from the corrected labels (`_tags.py:561` grammar), write via `apply_tags_*` on the `enrich`
   re-tag→`_verify_copy`→journal provenance chain — with `dry_run` and idempotency.  Consumes
   C-CAT-INT.  Not run destructively.
3. **S3 ◆ — Fresh scan gate + census refresh + register anneal.**  New scanner for `NN - NN`
   intermediate dirs / bare-catalogue `CWP_PART_*`; refresh the stale "1 release fired / ~16 latent"
   census against the current library (distinguish scan-not-run from no-findings); validate the
   repatch against a representative fixture; close the sub-track; anneal the planning register.

## Verify gate

Discovered from `pyproject.toml` (tox envs); do not assume `make`.  Both **binding** — this is a code
sub-track.  (Confirm green at shard time before S1.)

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage enforced**,
  `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict).
- Full gate before any row is declared done: `~/.local/bin/tox -m analyze` (build + test + check_type +
  check_format + check_lint 10.00/10 + check_upgrade).  The AGENTS.md "never skip `tox -m analyze`" rule
  applies to every row.  Import order via `~/.local/bin/tox -m edit`, never hand-edited.
- **S3 scan step is not gate-covered:** the new scanner lives outside `src/`+`tests/` (like
  `scan_nonuniform_depth.py` / `scan_fragmentation.py` / `census_original.py`); it runs clean under
  `venv/bin/python -m py_compile` and best-effort `venv/bin/mypy scripts/` but is not `tox`-enforced.
  Its gating role is producing a fresh scan the S3 ◆ review consumes, not passing the gate.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 @architect | Detect corrupt catalogue-colon CWP_PART labels and re-derive them offline | A | Opus | `strip_common_prefix` (NORM-9 forward fix), STYLEGUIDE 4.x | `src/music_annotator/_works.py`, `src/music_annotator/_tags.py`, `tests/unit/test_annotator.py` |
| 2 | Rewrite corrupt CWP_PART_*/CWP_GROUPHEADING in an offline repatch pass | B | Sonnet | **C-CAT-INT** | `src/music_annotator/_pipeline_maint.py`, `src/music_annotator/_tags.py`, `tests/unit/test_pipeline_maint.py` |
| 3 ◆ | Scan the library for corrupt catalogue-colon labels + census + anneal | I | Sonnet | **C-CAT-INT**, `scan_catalogue_colon.py` | `scripts/scan_catalogue_colon.py`, `docs/BACKLOG.md`, `tests/unit/test_pipeline_maint.py` |

`Cat`: **S1 is A (substrate)** — freezes **C-CAT-INT**, the detect-predicate + offline re-derivation
that S2's repatch pass and every future consumer read; over-specify (carry the general
"disagreement with the recomputed label" predicate and the `CWP_WORK`-pair re-derivation even though
S2 is the first consumer).  **S2 is B** — the tag-content write-back mechanics over the frozen
predicate, modelled on the existing `enrich` provenance chain.  **S3 is I (integrative)** — the
fresh-scan gate + census refresh give the contract its operator-visible/durable form (the scan is what
R6d's repatch will run against), close the ◆, carry the anneal.

`Tier`: **S1 is Opus + `@architect` inflection.**  The detection predicate is permanent policy over
the whole library and must not false-positive on a legitimately-short label (a real one-word movement
title vs a bare catalogue fragment) — the disagreement-based predicate and the "no MB call, re-derive
from the embedded `CWP_WORK` pair" ruling are the S1 judgments tests alone cannot catch (lever 3:
design-error cost; lever 4: correctness-criticality — a false-positive rewrites a *correct* label).
**S2, S3 are Sonnet** — mechanical over a frozen predicate with a strong inner loop and an existing
write-pass precedent (`enrich`) to model (lever 5: 100% branch coverage + strict mypy).
`juncture-tier: opus` — kept (arc default).

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.

- **S1 ≈ 120–200 LOC, 2–3 files** (detect predicate + offline re-derivation helper + KATs).  Within
  band.  **Irreducible unit (lever 2, floor):** the predicate and the re-derivation are one contract —
  a predicate that says "corrupt" without a correct value to compare against is undefined; the
  re-derivation with no predicate to trigger it is dead code.  Kept whole.  **Lever 3/4:** high
  cost-of-wrong / correctness-crit is *why* S1 is Opus+inflection, not why it fractures.  One-line
  title: "Detect corrupt catalogue-colon CWP_PART labels and re-derive them offline" — passes.
- **S2 ≈ 150–250 LOC, 3 files** (the offline repatch pass: apply re-derivation, rewrite `CWP_PART_*`,
  rebuild `CWP_GROUPHEADING`, write-back on the `enrich` provenance chain, `dry_run`/idempotency +
  tests).  Within band.  **Separate session by the one-line-commit-title corollary** — "run the
  repatch" is distinct from "define what's corrupt and what's correct"; split legitimately at the
  contract-sharp C-CAT-INT boundary (S1 freezes the predicate S2 consumes).  **Lever 1 (ambient
  complexity):** the first tag-content-mutation maintenance pass — but `enrich` is a direct precedent
  (offline re-tag→`_verify_copy`→journal), so this is not greenfield; not fractured below the floor.
- **S3 ≈ 60–120 LOC + scan run, 2–3 files** (new scanner + census refresh + a no-regression repatch
  parity test + anneal).  Under band; **separate by the corollary** — the scan/census/anneal is one
  integrative unit; merging into S2 yields an "and"-joined title.  Not fractured below the floor (the
  scan validates the population the census reports).

## Session detail

### S1 @architect — Detect corrupt catalogue-colon CWP_PART labels and re-derive them offline — freezes C-CAT-INT

**Deliverable.**  A pure, offline detect-and-re-derive substrate:
- **Re-derivation helper** (`_works.py` near `strip_common_prefix`, or `_tags.py` near
  `build_cwp_tags`): given a file's embedded `CWP_WORK_{i}` / `CWP_WORK_{i+1}` titles, recompute the
  correct level-`i` part label as `strip_common_prefix(CWP_WORK_i, CWP_WORK_{i+1})` — the exact call
  `build_cwp_tags` makes at `_tags.py:526`.  Total, pure, no I/O, no MB call.  When the `CWP_WORK`
  pair is absent, return a sentinel "cannot recompute" (do not fabricate).
- **Detection predicate:** a `CWP_PART_{i}` is corrupt iff it differs from the recomputed label **and**
  the difference has the catalogue-colon signature (the stored label is a bare fragment of what the
  recomputed label yields — i.e. the old bare-`":"` split truncated it).  Bound the predicate to the
  catalogue-colon signature so it does not become a general "re-derive every label" pass (that is
  R6d's one-pass scope, not this shard's).
- Docstrings state the property (detect a label the pre-`": "` split corrupted; re-derive offline from
  the embedded `CWP_WORK` pair), never the plan coordinate.

**KAT (the freeze witness for C-CAT-INT).**  In `test_annotator.py`:
(a) **Haydn Hoboken clamp-down of the bug** — a file with `CWP_WORK_1 = "String Quartet …, Hob. III:31"`,
`CWP_WORK_2 = "String Quartets, op. 20"`, corrupt `CWP_PART_1 = "31"` → predicate fires; re-derivation
yields the full corrected label (the shipped `strip_common_prefix` output), not `"31"`;
(b) **legitimately-short label preserved** — a file with a genuinely one-word correct label that equals
its own recomputed value → predicate does **not** fire (no false-positive rewrite);
(c) **colon-space label preserved** — a correct `Title: Movement` label (`": "`) recomputes to itself →
no fire;
(d) **`CWP_WORK` pair absent** — the helper returns "cannot recompute"; the predicate does not fire and
the tag is left untouched (the safe branch);
(e) **`CWP_GROUPHEADING` corruption detectable** — the same disagreement is visible in the groupheading
segments (S2 rebuilds it; S1 proves the segment-level re-derivation).

**Subtleties.**
- **The false-positive inflection (the `@architect` judgment).**  The predicate must fire on the
  bug's output and **only** on it — a real short movement title (e.g. `"Gigue"`, `"Coda"`) must not be
  rewritten.  Ruling to make and freeze at S1: detect by **disagreement with the recomputed label**,
  not by "looks like a number / matches a catalogue pattern".  Because the recomputed label *is* the
  authority (the shipped forward fix), a correct label recomputes to itself and cannot false-positive;
  a corrupt label recomputes to the full title and fires.  Confirm this against the census signatures
  (Hob./BWV/HWV) before freezing; a shape where a *correct* label disagrees with its recomputation is
  the reopen trigger (the forward fix, not just this interface, would be suspect).
- **Offline, no MB call.**  The re-derivation is a pure function of embedded `CWP_WORK` titles —
  mirror R6a's "the rule's structure moots the network question" resolution: here the source data is
  in the file, so no fetch is needed.
- **Over-specify per Category-A.**  Carry the general disagreement predicate and the `CWP_WORK`-pair
  re-derivation now even though S2 is the first consumer over a ~16-release population — a future
  full-re-derivation consumer (R6d) or a new signature will want them.
- **100%-branch-coverage gate.**  The fire branch, the no-fire branch, the cannot-recompute branch,
  and any `match/case` over signature need explicit tests (`case _: # pragma: no cover` if exhaustive).

**Deferrals.**  No write-back / repatch pass (S2); no fresh scan / census (S3); no destructive repatch
(R6d).

### S2 — Rewrite corrupt CWP_PART_*/CWP_GROUPHEADING in an offline repatch pass

*(Lower-fidelity sketch — correct for a post-substrate row; crisply specified after C-CAT-INT freezes at S1.)*

**Deliverable.**  A new offline maintenance pass (in `_pipeline_maint.py`, modelled on `enrich`) that:
- Resolves current on-disk paths via the journal lineage (`_resolve_current_lib`), reads each FLAC/MP3's
  embedded tags, applies the S1 predicate; for each corrupt `CWP_PART_{i}`, rewrites it to the
  re-derived label and **rebuilds `CWP_GROUPHEADING`** from the corrected part labels using the
  `build_cwp_tags` grammar (`_tags.py:561`, `" :: ".join(...)`) — reuse that grammar, do not mint a
  second groupheading assembler.
- Writes via `apply_tags_flac` / `apply_tags_mp3` on the **`enrich` provenance chain**: re-tag →
  `_verify_copy` (tag round-trip) → append a journal entry only after verification (a new `action`,
  e.g. `"repatched"`).  Idempotent (a second run on a corrected library is a no-op) and `dry_run`-aware
  (log planned repatches, write nothing).
- **Not run destructively on the live library** — the pass is proven on fixtures; R6d drives it under J3.

**KAT (behavioural witness).**  A fixture FLAC/MP3 with the corrupt Haydn tags → after the pass,
`CWP_PART_1` and `CWP_GROUPHEADING` read back corrected; `build_dest_path` on the corrected tags now
renders `NN - <full label>` (the path fix follows the tag fix); a `dry_run` run writes nothing; a
second run is a no-op (idempotency); a file with no corruption is untouched (no-regression).

**Subtleties.**
- **Model on `enrich`, don't invent.**  `enrich` is the existing offline tag-content write pass
  (P-FP3/P-FP4: idempotent, `dry_run`, re-tag→`_verify_copy`→journal).  Reuse that shape — this
  de-risks the "first tag-content-mutation pass" concern (lever 1): there is a precedent.
- **The path fix is a consequence, not a separate step.**  Once the embedded `CWP_PART_*` is
  corrected, `build_dest_path` renders the right label automatically (it reads the tag verbatim), so a
  subsequent `repath` (R6d) produces the corrected directory.  S2 need not repath — it fixes the tags;
  R6d repaths.
- **Provenance chain is load-bearing.**  Do not append the `"repatched"` journal entry before
  `_verify_copy` confirms the round-trip — the same confirmation-provenance invariant `enrich` obeys.
- **match/case coverage.**  Cover repatch-applied, dry-run, no-corruption, and cannot-recompute
  outcomes.

**Deferrals.**  No fresh scan / census (S3); no destructive library run (R6d).

### S3 ◆ — Scan the library for corrupt catalogue-colon labels + census + anneal

*(Lower-fidelity sketch — post-substrate integrative row.)*

**Deliverable.**  Validate the fix's population and refresh the stale census:
- New `scripts/scan_catalogue_colon.py` (standalone, `scan_nonuniform_depth.py` precedent): scan the
  **complete library** for `NN - NN` intermediate dirs and for any embedded `CWP_PART_*` the S1
  predicate flags as corrupt.  **Distinguish scan-not-run** (unmounted/empty root → never report clean)
  **from no-findings** (the R4b D-1 / R6a D-3 hazard); if unmounted at execution, record the census as
  *not run* and note the refresh pending.
- Refresh the stale BACKLOG figure ("bug fired on 1 release; ~16 Haydn + Bach/Handel latent") to the
  current library — BACKLOG:255 is explicit that the census "must be re-run, not assumed to be the
  single Angeles release."  A signature the S1 predicate mis-detects (a correct label flagged, or a
  corrupt one missed) is the reopen trigger — surface as a discovery; do not silently absorb.

**KAT.**  A no-regression parity test asserting the S1/S2 detect+repatch behaviour still holds against a
representative Haydn Hoboken fixture (the integrative session's behavioural pin).

**Subtleties.**  No `src/` change in S3 unless a scanner helper is promoted (it should not be — keep the
scanner standalone per the `scan_fragmentation.py` precedent).  Purely a scan-validation + census +
anneal row; **no destructive library operation** (R6d runs the repatch under J3).

**◆ boundary (register anneal).**  Re-read Purpose.  Confirm all three sessions enacted, `tox -m analyze`
green, ledger complete.  **Planning-register anneal:**
- Durable files (`_works.py`, `_tags.py`, `_pipeline_maint.py`, `scan_catalogue_colon.py`
  docstrings/comments) carry **no plan coordinates** — no "S1/S2/S3", no "R6b", no "catalogue-colon
  sub-track", no `/plan-run` vocabulary.  State the property/reason/invariant (e.g. "re-derive the part
  label offline from the embedded CWP_WORK pair per the `": "` split, NORM-9 / STYLEGUIDE 4.x"), never
  the plan coordinate.
- Grep the durable files against the **anneal denylist** (Notes for executors); translate any leaked
  coordinate into standalone prose.
- Report to the library-completion roadmap: the offline tag-content-repatch machinery is enacted;
  C-CAT-INT frozen.  **R6d coordination noted** — the machinery closes the R6d tag-content gap for the
  catalogue-colon case; R6d runs the destructive library-wide repatch under J3 (this sub-track lands
  the machinery, not the destructive run).

## Cross-session contracts

### C-CAT-INT — the catalogue-colon detect + offline re-derivation interface *(FROZEN at S1 — inflection design)*

**Detect predicate + offline re-derivation (frozen at S1).**  A `CWP_PART_{i}` is corrupt iff it
differs from `strip_common_prefix(CWP_WORK_i, CWP_WORK_{i+1})` recomputed under the shipped `": "`
rule **and** the difference has the catalogue-colon signature (stored label is what the *old
bare-`":"` split* would have produced from the recomputed label).  The re-derivation is a **pure
function of the embedded `CWP_WORK` title pair — no MB network call**; when the level-`i` `CWP_WORK`
title itself is absent it returns a "cannot recompute" sentinel and the predicate does not fire (the
safe branch — never fabricate a label).  Note an *absent parent* (`CWP_WORK_{i+1}`) is not a
failure — it is the root/top level, where `strip_common_prefix(child, "")` returns `child` unchanged,
a valid recomputation.  **Invariant:** a *correct* label recomputes to itself, so the
predicate cannot false-positive on a legitimately-short movement title (the S1
correctness-criticality judgment).  Deterministic and total.

#### Resolved interface (frozen — implement exactly; no further design decisions)

**Module.**  Both symbols live in **`src/music_annotator/_works.py`**, immediately after
`strip_common_prefix` (their only dependency; keeps the wrapper colocated with the shipped forward
fix and adds no new cross-module import — `_tags.py` and `_pipeline_maint.py` already import from
`_works.py`).

**Typed sentinel.**  A module-level `enum.Enum` singleton — the idiomatic typed sentinel that
narrows cleanly under mypy-strict `is`/`match` checks:

```python
import enum
from typing import Final

class _Rederivation(enum.Enum):
    """Sentinel domain for :func:`rederive_part_label` when the label cannot be recomputed."""

    CANNOT_RECOMPUTE = enum.auto()

#: Returned by :func:`rederive_part_label` when the embedded ``CWP_WORK`` pair is incomplete, so no
#: offline recomputation is possible (the safe branch — the caller must leave the stored label
#: untouched, never fabricate one).
CANNOT_RECOMPUTE: Final = _Rederivation.CANNOT_RECOMPUTE
```

Export `CANNOT_RECOMPUTE` (and both functions below) from `__init__.py` `__all__` if the S1 KATs
import them at package level (they may instead import from `music_annotator._works` — either is
acceptable; match the surrounding test-import convention in `test_annotator.py`, which imports pure
helpers from both the package root and `music_annotator._works`).

**Re-derivation helper.**

```python
def rederive_part_label(child_title: str, parent_title: str) -> str | _Rederivation:
    """Recompute the correct level-i part label offline from the embedded CWP_WORK pair.

    Recomputes the label as ``strip_common_prefix(child_title, parent_title)`` — the exact call
    ``build_cwp_tags`` makes at build time — so the result is identical to what a fresh ingest under
    the shipped ``": "`` rule (NORM-9 / STYLEGUIDE 4.x) would produce.  Pure, total, no I/O, no MB
    network call: the source titles are already embedded in the file as ``CWP_WORK_{i}`` /
    ``CWP_WORK_{i+1}`` alongside the (possibly corrupt) ``CWP_PART_{i}``.

    :param child_title: The level-i work title (embedded ``CWP_WORK_{i}``).
    :param parent_title: The level-(i+1) parent work title (embedded ``CWP_WORK_{i+1}``); ``""`` at
        the root, where no parent exists within the hierarchy.
    :returns: The recomputed part label string, or :data:`CANNOT_RECOMPUTE` when ``child_title`` is
        empty (nothing to recompute) — never a fabricated label.
    """
```

Contract detail (frozen):

- **`child_title == ""` → `CANNOT_RECOMPUTE`.**  With no level-i title embedded there is nothing to
  recompute; the caller leaves the stored tag untouched (D-5 safe branch).  This is the sole
  cannot-recompute trigger.
- **`parent_title == ""` is NOT cannot-recompute.**  A root/top level legitimately has no parent;
  `strip_common_prefix(child, "")` returns `child` unchanged (its `not parent` guard) — a valid
  recomputation, not a failure.  This matches `build_cwp_tags`'s own `parent_name = ""` at the top
  level (`_tags.py:525`).
- The helper is a thin, faithful wrapper over `strip_common_prefix` — it does **not** re-implement
  or re-open the split rule (frozen upstream: NORM-9).

**Detection predicate.**

```python
def is_catalogue_colon_corrupt(stored_label: str, child_title: str, parent_title: str) -> bool:
    """Return True iff a stored CWP_PART label was corrupted by the pre-fix bare-":" split.

    Fires iff BOTH hold: (1) the stored label disagrees with the offline recomputation
    ``rederive_part_label(child_title, parent_title)`` under the shipped ``": "`` rule, AND (2) the
    disagreement carries the catalogue-colon signature — the stored label is exactly what the *old*
    bare-``":"`` split would have produced from the recomputed label (i.e. the split truncated at a
    catalogue colon such as Hoboken ``"Hob. III:31"``).  The signature bound is load-bearing: it
    scopes the pass to the catalogue-colon bug and keeps it from becoming a general
    "re-derive every label" pass (that is deferred).

    A correct label recomputes to itself → clause (1) is False → cannot false-positive on a
    legitimately-short movement title.  When recomputation is impossible (:data:`CANNOT_RECOMPUTE`)
    the predicate returns False (the safe branch — the caller leaves the tag untouched).

    :param stored_label: The embedded ``CWP_PART_{i}`` value read back from the file.
    :param child_title: The embedded ``CWP_WORK_{i}`` value.
    :param parent_title: The embedded ``CWP_WORK_{i+1}`` value (``""`` at the root).
    :returns: True iff the stored label is a catalogue-colon-corrupt label to be re-derived.
    """
```

Predicate algorithm (frozen — three branches, each explicitly test-covered per the 100%-branch gate):

1. `recomputed = rederive_part_label(child_title, parent_title)`.
2. **cannot-recompute branch:** `if recomputed is CANNOT_RECOMPUTE: return False` (D-5 safe branch;
   narrow the `str | _Rederivation` union via `is`).
3. **no-disagreement branch:** `if stored_label == recomputed: return False` (correct labels
   recompute to themselves → no false-positive).
4. **signature branch:** `return _old_bare_colon_split(recomputed) == stored_label` — fires iff
   re-running the *old* bare-``":"`` split on the recomputed label reproduces the stored corrupt
   label.  This is the self-certifying catalogue-colon signature: it fires exactly where the old bug
   fired and nowhere else (verified at design time against the Haydn Hoboken fixture and the
   colon-space / legitimately-short / non-catalogue-disagreement negatives).

**Private helper (the old-bug reproducer, the signature core).**

```python
def _old_bare_colon_split(label: str) -> str:
    """Reproduce the pre-fix bare-":" split output for signature detection only.

    Returns the fragment the retired bare-colon fallback in ``strip_common_prefix`` produced:
    everything after the first bare ``":"``, stripped.  This exists solely to recognise a label the
    pre-``": "`` split corrupted (by comparing this reproduction to the stored label); it is NOT the
    forward path and must not be used to derive any written label.  Corrects nothing — it only
    witnesses the old corruption.

    :param label: The recomputed (correct) part label to run the old split against.
    :returns: The fragment after the first bare ``":"``, stripped; or ``label`` unchanged when no
        bare colon is present or the split would yield an empty string.
    """
    idx = label.find(":")
    if idx != -1:
        after = label[idx + 1 :].strip()
        return after if after else label
    return label
```

Note: this reproducer keys on a **bare `":"`** (`label.find(":")`), deliberately *not* `": "` — it
models the *retired* behaviour, and reproducing that old behaviour is the whole point of the
signature.  It does not re-open NORM-9; the forward path (`rederive_part_label` →
`strip_common_prefix`) still uses only `": "`.

**Groupheading segment re-derivation (KAT (e)) — no new symbol.**  `CWP_GROUPHEADING` is
`" :: ".join(...)` over the top-work title and the per-level part labels (`_tags.py:561`–`571`).  A
groupheading segment is corrupt exactly when its underlying `CWP_PART_{i}` is corrupt — so KAT (e)
is proven by applying `is_catalogue_colon_corrupt` at the *segment/label level*, not by a new
groupheading-specific detector.  S1 proves the segment-level re-derivation (a corrupt `CWP_PART_{i}`
implies the matching `" :: "` segment is corrupt and re-derives to the corrected label); S2 rebuilds
the full `CWP_GROUPHEADING` string via the existing `build_cwp_tags` grammar.  **No second
groupheading assembler at S1.**

**Inputs are the embedded string tags, not `MBWork` objects.**  Both functions take plain `str`
titles read from the file's tag dict (uppercase `CWP_WORK_{i}` / `CWP_PART_{i}` keys via
`_read_flac_tags` / `_read_mp3_tags` → `file_dict: dict[str, str]`).  They take a single level's
title pair (not a whole dict) so they are per-level, total, and directly unit-testable at the
segment granularity KAT (e) needs; the dict-walk that pairs `CWP_WORK_{i}` with `CWP_WORK_{i+1}`
across all levels of a file is **S2's** concern (the repatch pass), not S1's.  **Over-specified per
Category-A:** the general disagreement recomputation and the `CWP_WORK`-pair signature are carried
now though S2 is the first consumer.

**Type aliases.**  None required beyond the `_Rederivation` enum sentinel above; the public return
type is the inline union `str | _Rederivation`.

**Posture (to be frozen at S1).**  Detect by **disagreement with the recomputed label**, not by a
catalogue-fragment regex (the forward fix is the authority on the correct label).  Re-derive **offline
from embedded tags**, not from MB (the source data is in the file).  Bound to the catalogue-colon
signature (broader re-derivation is R6d's one-pass scope).  The existing library re-derives via the S2
repatch pass driven by R6d's one J3-gated pass; temporary corruption on disk until then (D-A4/D-A6-style,
accepted).

**Flavour:** compiler-enforced (the re-derivation helper + predicate signatures; mypy strict) +
test-enforced (the S1 KATs: bug-fires, short-label-preserved, colon-space-preserved,
cannot-recompute, groupheading-segment; the S2 repatch/dry-run/idempotency/no-regression KATs) +
prose-enforced (the disagreement-not-regex rule and the no-false-positive invariant, cited to NORM-9 /
STYLEGUIDE 4.x / the forward fix in `strip_common_prefix`).  **Defined-in:** S1.  **Consumed-by:** S2
(the repatch pass), S3 (scan validation), R6d (the one-pass drives the S2 pass destructively), any
future full-re-derivation consumer.  Over-specified per Category-A: carries the general disagreement
predicate and the `CWP_WORK`-pair re-derivation though S2 is the first consumer over a ~16-release
population.

### Consumed (frozen upstream — invalidation is out of scope for this sub-track)

- **NORM-9 / `strip_common_prefix` `": "` rule (STYLEGUIDE-ratified)** — the forward fix.  R6b builds
  the *retro-fix* over it; it does **not** re-open the split rule.  A shape where a correct label
  disagrees with its recomputation is a finding for the arc boundary (forward-fix reopen trigger), not
  an in-arc rule change.
- **C-W3b-INT (R6a)** — the depth clamp.  R6b changes part-label *content*, never depth; the two are
  orthogonal (depth = how many levels render; label = the string per level).  Validate-only.
- **C-CLASS / C-INIT** — class scheme + within-classical component.  The repatch changes labels *below*
  `work_dir`, never the class/top_dir structure.  Validate-only.
- **C-L0 / C-L1** — leaf/intermediate numbering.  The repatch changes the *label* after the `NN - `
  prefix, never the numbering grammar.  Validate-only.
- **C-PROV / C-MOVE + confirmation-provenance** — move/verify/journal provenance.  S2's write-back
  rides the `enrich` re-tag→`_verify_copy`→journal chain unchanged; the `"repatched"` entry is
  appended only after verification.  Validate-only — preserve the chain exactly.
- **"Path is a handle, not a manifest"** — the repatch corrects the label the path carries, not the
  path's identity role.

### Produced

- **C-CAT-INT** — the detect + offline re-derivation interface at S1; the repatch pass at S2; scan
  validation at S3.  **Coordinates with R6d** (the destructive library-wide repatch): the machinery is
  landed here; R6d runs the S2 pass destructively under J3 — and this **closes the ROADMAP R6d
  tag-content gap** for the catalogue-colon case (R6d's paths-only engine gains a tag-content-repatch
  capability).

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 @architect | Detect corrupt catalogue-colon CWP_PART labels and re-derive them offline | done | 1f5d76a | C-CAT-INT (frozen) |
| 2 | Rewrite corrupt CWP_PART_*/CWP_GROUPHEADING in an offline repatch pass | done | 317bf46 | |
| 3 ◆ | Scan the library for corrupt catalogue-colon labels + census + anneal | done | 16d31db | sub-track complete; ◆ still-on-intent |

## Action-frame digest

- **S1 inflection design (juncture adjudicator, `design-confident`).**  C-CAT-INT frozen with the
  concrete interface: `rederive_part_label(child_title, parent_title) -> str | _Rederivation` and
  `is_catalogue_colon_corrupt(stored_label, child_title, parent_title) -> bool` in `_works.py`, with
  the `CANNOT_RECOMPUTE` enum sentinel and the `_old_bare_colon_split` signature helper.  Key design
  ruling: the catalogue-colon signature is **self-certifying** — the predicate fires iff re-running
  the *retired* bare-`":"` split on the recomputed label reproduces the stored label.  Verified at
  design time against the Haydn Hoboken fixture (fires) and the colon-space / legitimately-short /
  non-catalogue-disagreement negatives (all no-fire).  D-1 confirmed-resolved; no reopen trigger
  found.  Load-bearing assumption: the embedded `CWP_WORK` titles are intact (D-5, survey-confirmed).

### S3 ◆ — 2026-08-13
Discovery/flex: Boundary-transform fork returned `still-on-intent`; all three sessions realized the design intent and frozen contracts exactly.
Affected: none
Deferred: no — live-population re-check (D-2 reopen trigger) still owned by the operator-run scan before R6d's destructive pass.
Texture: Anneal fix applied opportunistically to a pre-existing roadmap coordinate in `_tags.py:252` (translated to standalone prose); all sub-track durable files clean of plan coordinates.

## Discoveries & risks

- **D-1 (S1 false-positive detection — the inflection judgment; CONFIRMED-RESOLVED at S1 inflection
  design).**  The predicate must fire on the bug's output and only on it.  Resolution frozen at S1:
  detect by **disagreement with the recomputed label** (the shipped `strip_common_prefix` is the
  authority), so a correct label recomputes to itself and cannot false-positive; the disagreement is
  qualified by the **self-certifying catalogue-colon signature** (`_old_bare_colon_split(recomputed)
  == stored_label`) so the pass fires exactly where the old bug fired and nowhere else — a general
  hand-edit disagreement does **not** fire (scope-bounded per posture 2).  Verified at design time
  against the census signatures (Hoboken; colon-space and legitimately-short negatives).  No shape
  was found where a *correct* label disagrees with its recomputation.  *internal-continue* — the
  reopen trigger remains a census signature where a correct label disagrees (a **destructive-HALT /
  forward-fix-reopen** signal), to be re-checked against the live population at the S3 scan.
- **D-2 (fresh-scan population — additive-reshard signal).**  BACKLOG:255: the "1 release fired / ~16
  latent" figure is stale by construction and "must be re-run, not assumed to be the single Angeles
  release."  If the fresh S3 scan surfaces a **new corruption signature** the disagreement predicate
  mis-handles, or a much larger population, that is a reopen trigger — surface it; do **not** absorb
  it in-track.  *additive-reshard* (a new-signature row) or *destructive-HALT* (predicate wrong),
  decided live at the S3 scan.
- **D-3 (host-path silent-no-op hazard — carried from R6a D-3 / R4b D-1).**  The new scanner's `ROOT`
  is machine-specific (`~/Remote/hades/Music/Done`, the `scan_nonuniform_depth.py` pattern).  S3
  **must** distinguish scan-not-run (unmounted/empty root → never "clean") from no-findings.  Operator
  mounts the library before `/plan-run`; if unmounted at execution, the census refresh is recorded
  pending, not asserted.  *internal-continue* (S3 handles it structurally).
- **D-4 (R6d coupling — sequencing constraint, not a risk).**  This shard builds the machinery;
  the destructive library-wide repatch is R6d's one J3-gated pass (D-A5/D-A7).  The S2 pass is the
  machinery R6d drives — and it **closes the R6d tag-content gap** for the catalogue-colon case.  No
  destructive op in this sub-track.  *internal-continue.*
- **D-5 (`CWP_WORK`-pair dependency — accepted).**  The offline re-derivation relies on the embedded
  `CWP_WORK_{i}` titles being intact.  The survey confirms they are always written alongside `CWP_PART`
  (`_tags.py:1015–1022`) and only the *derived* `CWP_PART` was ever corrupted — never the source
  `CWP_WORK`.  If a file lacks the pair the repatch leaves the tag untouched + does not fire (the safe
  branch).  Noted so `/plan-run` does not treat the cannot-recompute branch as a defect.
  *internal-continue.*
- **D-6 (temporary library inconsistency — accepted, D-A4/D-A6-style).**  Until R6d's repatch, the
  on-disk library mixes corrected (new ingests) and corrupt (the ~16 latent releases) part labels.
  Accepted by the operator (posture 3); not a defect to remediate in-track.  Noted so `/plan-run` does
  not treat it as an in-track discovery.

## Notes for executors

- **Tier routing.**  S1 is **Opus + `@architect` inflection** (the C-CAT-INT detect-predicate +
  offline re-derivation judgment; permanent library-wide policy; correctness-crit — a false-positive
  rewrites a correct label).  S2, S3 are **Sonnet** (mechanical over the frozen predicate, modelled on
  `enrich`).  `juncture-tier: opus` — kept.
- **Register: retro-fix over the forward fix, don't re-open it.**  NORM-9 / the `": "` split is
  ratified; R6b builds the *retro-fix* of already-corrupt tags.  If a row seems to *need* a split-rule
  change (a correct label disagrees with its recomputation), that is a **discovery / forward-fix
  reopen** (surface it), not a licence to re-adjudicate the split in-track.
- **Detect-by-disagreement is load-bearing.**  The predicate rewrites only labels that disagree with
  the recomputed value under the shipped rule — a correct label recomputes to itself.  Every repatch
  test must carry a legitimately-short-label case asserting no false-positive rewrite.
- **Offline, no MB call.**  The re-derivation is a pure function of embedded `CWP_WORK` titles.  A
  version that fetches from MB violates the posture (and re-opens identity questions this fix does not
  touch).
- **Model S2 on `enrich`, not fresh.**  `enrich` (`_pipeline_maint.py:1254`) is the existing offline
  tag-content write pass: idempotent, `dry_run`, re-tag→`_verify_copy`→journal (P-FP3/P-FP4).  Reuse
  that shape (a new `action="repatched"` entry appended only after verification).  Do not mint a second
  groupheading assembler — reuse the `build_cwp_tags` `" :: ".join(...)` grammar (`_tags.py:561`).
- **REGISTER rule (durable-file discipline).**  In source/tests, state the *property/reason/invariant*
  — never the plan coordinate.  "re-derive the part label offline from the embedded CWP_WORK pair per
  the `": "` split (NORM-9 / STYLEGUIDE 4.x)" is right; "the S1 detect predicate" is not.  Plan
  vocabulary (S1/S2/S3, R6b, sub-track names, `/plan-run`) lives only in `PLAN.md` / `ROADMAP*.md` /
  the ledger / commit messages.  See the repo `AGENTS.md` "Register rule" block.
- **Anneal denylist (◆ gate greps durable files for these).**  Seeded from the `/plan-run` default,
  tuned for this project's vocabulary:
  - `\bS[1-9]\b` (this sub-track's plan session coordinates) — **but** allow STYLEGUIDE-rule-section
    forms (`\b[1-5]\.[0-9]\b` like "4.5", "3.1" are register/rule cites, not plan coordinates — do
    **not** flag).
  - `\bR6[a-e]\b`, `\bR[0-9]\b` (roadmap node coordinates) — flag in durable source/tests; legitimate
    only in PLAN/ROADMAP/ledger/commit messages.
  - `sub-track`, `plan-run`, `plan-shard`, `halt-at-boundaries`, `run-to-boundary`
  - `C-CAT-INT` **only outside docstrings that legitimately name the contract** — contract names in
    docstrings are the intended durable form; flag bare "S1 freeze"-style prose, not the contract name.
  - `juncture`, `inflection`, `action-frame`, `◆`
  - Do **not** add `catalogue`, `CWP_PART`, `CWP_GROUPHEADING`, `CWP_WORK`, `strip_common_prefix`,
    `Hoboken`, `NORM-9`, `repatch`, `": "` to the denylist — these are legitimate domain/rule
    vocabulary this sub-track deliberately renders and cites.
- **Invariants to preserve:** the detect-by-disagreement / no-false-positive rule (C-CAT-INT); the
  offline / no-MB-call re-derivation; the `enrich` re-tag→`_verify_copy`→journal
  confirmation-provenance chain (S2 rides it unchanged — the `"repatched"` entry appended only after
  verification); C-CLASS/C-INIT (class/top_dir unchanged — repatch acts below `work_dir`); C-L0/C-L1
  (numbering grammar unchanged); C-W3b-INT (depth unchanged — orthogonal to label content); "path is a
  handle, not a manifest".
- **Every row runs `~/.local/bin/tox -m analyze` before ledger-done** (build + test at 100% branch
  coverage + strict mypy + ruff + pylint 10.00/10 + pyupgrade).  Import order via
  `~/.local/bin/tox -m edit`, never hand-edited.
- **Suggested first `/plan-run` invocation:** `halt-at-boundaries` — the C-CAT-INT detect predicate
  (the disagreement-not-regex ruling + the no-false-positive invariant + the offline re-derivation) is
  the first unproven substrate judgment in this shard; stop after S1 for an operator check that the
  freeze (especially that a legitimately-short label cannot be false-positive-rewritten) is right
  before S2 consumes it.  Once S1 confirms, `run-to-boundary` through the S3 ◆.
