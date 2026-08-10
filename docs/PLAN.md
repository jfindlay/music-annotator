<!-- juncture-tier: opus -->
<!-- sub-track: sidecar-case-ids (post-v1 styleguide application) — rule 5.5 case-ID persistence on
     ProvenanceSidecar.  Records which contested-case (P2) neutral defaults were applied per release, so the
     applied editorial rulings survive in the provenance sidecar (claim-in-the-unit, prose-in-STYLEGUIDE).
     SIDECAR-ONLY: no persisted-tag or path change, so NO R6d coupling and no library-wide repath (distinct
     from the other two remaining node-A shards).  This IS a /plan-run target: mechanical model+pipeline+audit
     changes verifiable by the src/tests gate with zero library access. -->

# PLAN — sidecar-case-ids: rule-5.5 applied-case-ID persistence on ProvenanceSidecar

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

STYLEGUIDE rule 5.5 (contested-case marking): where releases or editors legitimately disagree and the
styleguide has chosen a neutral default (P2), *applying that default is itself an annotation-on-the-annotation*
— "implementations that maintain per-release provenance sidecars record the applied case-IDs there: claim in
the unit, prose in this document, nothing free-text in tags."  The code already maintains a provenance sidecar
(`ProvenanceSidecar`, C-TIER/C-AR) but records no case-IDs.  This sub-track closes that gap: it persists the
run-derived set of contested-default case-IDs that were *actually applied* for each release into the sidecar,
under the same C-PROV write-provenance discipline as `annotation_tier`.

No new editorial decisions are taken — the case-IDs are the *already-frozen* v1 rulings; this records which of
them bit for a given release.  The unifying principle is **the epistemic register** (STYLEGUIDE P3, layer 5):
every applied neutral default is a claim whose basis survives with the annotated unit, at zero side-channel
cost, without defacing any compact surface (5.3's ceiling carve-out → 5.5's sidecar mechanism).

The three sessions, in landing order:

1. **S1 — Model + merge substrate.**  Add `applied_case_ids: list[str]` to `ProvenanceSidecar` and a
   **set-union monotonic-append** merge arm in `_write_provenance_fields` (case-IDs accumulate across
   re-annotations; an incoming empty list never erases the recorded set).  Freezes **C-CASE-PROV**.
2. **S2 — Source + thread the applied set.**  Derive the run-scoped set of contested-default (P2) case-IDs
   actually applied for a release, accumulate them per work directory, and thread them to the C-PROV write
   site.  Consumes C-CASE-PROV.
3. **S3 ◆ — Audit surface + register anneal.**  Enumerate `applied_case_ids` in the `audit` tier pass
   (parallel to `annotation_tier`/`needs_spot_check`); close the sub-track; anneal the planning register.

## Verify gate

Discovered from `pyproject.toml` (tox envs); do not assume `make`.  Both are **binding** — this is a code
sub-track.  (Confirmed green at shard time: 1661 tests, 100.00% branch coverage.)

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage enforced**, `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict).
- Full gate before declaring any row done: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format
  + check_lint 10.00/10 + check_upgrade).  The `AGENTS.md` "never skip `tox -m analyze`" rule applies to every row.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 | Add applied_case_ids field and set-union merge to ProvenanceSidecar | A | Opus | C-TIER, C-AR, C-PROV | `src/music_annotator/models.py`, `src/music_annotator/_pipeline_io.py`, `tests/unit/test_models.py`, `tests/unit/test_pipeline.py` |
| 2 | Source and thread applied contested-default case-IDs into the sidecar | B | Opus | **C-CASE-PROV**, C-PROV, STYLEGUIDE 5.5/P2 | `src/music_annotator/_tags.py`, `src/music_annotator/_pipeline.py`, `src/music_annotator/models.py`, `tests/unit/test_pipeline.py` |
| 3 ◆ | Surface applied case-IDs in the audit tier pass | I | Sonnet | **C-CASE-PROV**, C-TIER | `src/music_annotator/_audit.py`, `tests/unit/test_audit.py` |

`Cat`: **S1 is A (substrate)** — it adds the persisted field + merge contract that S2/S3 and every future
sidecar reader assume; over-specify the merge semantics (set-union, append-only, empty-never-erases).  **S2 is
B** — it derives and threads a new per-release datum through the render/pipeline path.  **S3 is I
(integrative)** — it gives the contract its public/operator-visible form (the audit surface is "where the
contract becomes visible"), closes the ◆, and carries the register anneal.
`Tier`: **S1 and S2 are Opus.** S1 freezes a durable contract that sidecars persist on disk — a mis-freeze
(wrong field name, wrong merge semantics) is costly to revise after real sidecars carry it (lever 3, cost of a
design error).  S2 carries the *sourcing-model judgment* — which cases count as "applied," where the decision
sites are, and how to instrument them under the 100%-coverage gate without minting dead branches (lever 3 +
lever 1, ambient match/case coverage complexity).  **S3 is Sonnet** — a mechanical mirror of the existing
`_audit_tier_pass`; one clear surface, strong inner loop (lever 5) covers it.  `juncture-tier: opus` — kept
despite the sidecar-only / no-R6d-coupling posture, because the C-CASE-PROV freeze is durable and S2's
sourcing model is a judgment tests cannot fully catch.

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.

- **S1 ≈ 50–80 LOC, 4 files.**  Under the band by LOC but a genuine irreducible unit: the field and its merge
  arm are one contract (**lever 2, the floor** — a field with no merge rule is undefined under re-annotation;
  a merge rule with no field is nothing).  Kept whole; not split.  One-line-commit-title check: passes.
- **S2 ≈ 80–140 LOC, 3–4 files** (sourcing helper + per-work-dir accumulator + thread to write site + tests).
  The sourcing derivation and the threading are **one conceptual unit** (lever 2): you cannot thread a set you
  have not defined, and the set's definition (which cases, sourced how) *is* the interface the KAT witnesses.
  Splitting "define the set" from "thread the set" would strand a helper with no consumer under the coverage
  gate (lever 5 fails on the unreachable helper) — the same dead-code failure mode the A-shards S1 avoided.
  Kept whole.
- **S3 ≈ 30–50 LOC, 2 files.**  Under the band; a **separate session by the one-line-commit-title corollary** —
  "surface applied case-IDs in audit" is a distinct read-path surface with no shared implementation with S2's
  write path; merging it into S2 yields an "and"-joined title (thread AND surface).  Not fractured below the
  floor — it is already one irreducible unit (the audit pass + its coverage).

## Session detail

### S1 — Add applied_case_ids field and set-union merge to ProvenanceSidecar — freezes C-CASE-PROV

**Deliverable.**  `ProvenanceSidecar` gains `applied_case_ids: list[str]` (default `[]`), and
`_write_provenance_fields` gains a **set-union monotonic-append** merge arm mirroring the existing
`annotation_tier` / `accuraterip_summary` arms:
- `models.py`: add the field on `ProvenanceSidecar` (after `accuraterip_summary`, ~1848) with an rST attribute
  docstring stating the C-CASE-PROV semantics (applied contested-default case-IDs; append-only set-union;
  order-normalised for stable YAML).
- `_pipeline_io.py` `_write_provenance_fields` (~1562, after the AR arm): read `existing.get("applied_case_ids", [])`,
  union with `provenance.applied_case_ids`, write the **sorted** union when it differs from the existing set; an
  incoming empty list must never shrink or erase the recorded set (the empty-incoming guard).  Update the
  method docstring's idempotency-rules block with the new arm.
- `_read_provenance_sidecar` needs no change (Pydantic parses the list; absent key → default `[]`), but the
  round-trip must be covered.

**KAT (the freeze witness for C-CASE-PROV).**  In `test_pipeline.py`, over `_write_provenance_fields`:
(a) writing `applied_case_ids=["SEL-11","REND-14"]` to an empty sidecar records the sorted set;
(b) a second write with `applied_case_ids=["NORM-2"]` yields the **union** `["NORM-2","REND-14","SEL-11"]`
(append-only proof); (c) a write with `applied_case_ids=[]` leaves the recorded set **unchanged**
(empty-never-erases proof); (d) a `ProvenanceSidecar` round-trips through `_read_provenance_sidecar` after write
carrying the case-IDs.  Plus a `test_models.py` field-default test (`ProvenanceSidecar().applied_case_ids == []`).

**Subtleties.**
- **Set-union, not monotonic-tier:** unlike `annotation_tier` (a single rank-ordered value), case-IDs are a
  *growing set* — re-annotation may apply additional contested defaults; none are ever retracted at the sidecar
  layer.  Freeze this asymmetry in C-CASE-PROV explicitly (over-specify per Category-A).
- **Deterministic serialization:** always write the union **sorted**, so re-writes are byte-stable and the
  idempotency "file unchanged when set unchanged" property holds (the `_write_provenance_fields` YAML dump must
  not reorder nondeterministically).  Coverage: both the changed-set and unchanged-set branches tested.
- **100%-branch-coverage gate:** the new merge arm has an empty-incoming branch, a subset-incoming
  (no-change) branch, and a superset-incoming (write) branch — all three need explicit tests.
- **No production writer yet** (S2 wires it): S1's field is exercised only by unit tests until S2.  That is the
  standard substrate-first pattern — the field is populated *in the test*, so the coverage gate is satisfied
  without a live path.  (Confirm no `# pragma: no cover` is needed; the merge arm is fully reachable from tests.)

**Deferrals.**  No sourcing, no threading (S2); no audit surface (S3).

### S2 — Source and thread applied contested-default case-IDs into the sidecar

*(Lower-fidelity sketch — correct for a post-substrate row; crisply specified after C-CASE-PROV freezes at S1
and after the S2 inflection ruling on the exact source set.)*

**Deliverable.**  Derive the run-scoped set of contested-default (P2) case-IDs actually applied for a release
and thread it to the C-PROV write site (`_pipeline.py` ~1334) so it lands in `ProvenanceSidecar.applied_case_ids`.
- **Source set (the S2 juncture judgment — see Subtleties).**  The contested-default population is the
  register's P2/"contested by nature" cases whose neutral default the pipeline applies: candidates with real
  per-release decision sites include **SEL-11** (soloist not promoted to path — fires per concerto release),
  **NORM-1** (historical ensemble rename applied — fires when a rendered ensemble's canonical ≠ credited),
  **NORM-2** (native/Latin reception form chosen — fires per name-form selection), **REND-1** (composer not
  appended to `ARTIST`), **REND-2** (composer not prefixed to `ALBUM`), **REND-14** (billing-order composite).
  The juncture rules the exact set and, per case, whether it is *run-derived* (a decision site fired) or
  *structural* (always applied for a classical release).
- **Accumulation.**  Per work directory (matching the once-per-work-dir C-PROV write), accumulate the union of
  case-IDs applied across the dir's tracks — a run-level accumulator keyed on `work_top_dir`, parallel to
  `tier_written`.
- **Threading.**  Pass the accumulated set into the `ProvenanceSidecar(...)` construction at `_pipeline.py`
  ~1334/1336 alongside `annotation_tier` / `needs_spot_check`.

**KAT (behavioural witness).**  A `run()`-level test over a concerto release with a named soloist asserts the
work-dir sidecar's `applied_case_ids` contains `"SEL-11"` (the soloist-not-in-path default was applied); a
release whose ensemble has a canonical≠credited name asserts `"NORM-1"` present; a plain single-composer
release asserts the structural set (e.g. `"REND-1"`) present and the concerto-only `"SEL-11"` **absent**.

**Subtleties.**
- **This is the sourcing-model juncture** the Opus tier exists for.  The run-derived-vs-structural line per case
  is a judgment a test cannot fully adjudicate — the juncture (or operator) rules the exact source set before
  the row is implementable.  *C-CASE-PROV's "which cases" clause is "to be frozen at S2" until then.*
- **Layer-routing:** case-application provenance is *provenance*, not tag-rendering — keep the emission out of
  the on-disk `TrackTags`/`to_file_dict` (nothing free-text in tags, per 5.5) and route it through the pipeline
  accumulator to the sidecar, exactly as `annotation_tier` is kept in `_pipeline.py` not `_tags.py` (the C-TIER
  precedent).
- **match/case coverage:** instrumenting `build_cea_performers` / concerto / name-form decision sites must not
  mint unreachable arms; prefer emitting the case-ID at the point the default is *applied*, and cover both the
  applied and not-applied branches (lever 1, ambient coverage complexity).
- **C-CASE append-only interplay:** the emitted strings are register case-IDs — they must match the register
  exactly (`"SEL-11"`, not `"SEL11"`); a consolidation (e.g. NORM-10→NORM-2) means emitting the *survivor* ID.

**Deferrals.**  No audit surface (S3).

### S3 ◆ — Surface applied case-IDs in the audit tier pass

*(Lower-fidelity sketch — post-substrate integrative row.)*

**Deliverable.**  Extend `_audit_tier_pass` (`_audit.py` ~284) to read `applied_case_ids` from each work-dir
sidecar (the sidecar is already read + cached there) and surface it: add an `audit_tier_case_ids` log event (or
extend the existing per-tier events) reporting the applied case-IDs per work dir, and a count in the audit
summary parallel to `needs_spot_check`.  The `sidecar_cache` tuple (~351) extends to carry `applied_case_ids`.

**KAT.**  A `test_audit.py` case with a sidecar carrying `applied_case_ids=["SEL-11","REND-14"]` asserts the
audit pass logs/counts them; a sidecar with `applied_case_ids=[]` asserts no case-ID event (both branches covered).

**Subtleties.**  Mirror the exact dedup/eligibility guard the tier pass already uses (`action in {"tagged",
"enriched"}`, min-2-parts) so the case-ID denominator reconciles with `counts["total"]`.  Purely additive to the
audit surface — no change to write-path or contract.

**◆ boundary (register anneal).**  Re-read Purpose.  Confirm all three sessions enacted, `tox -m analyze` green,
ledger complete.  **Planning-register anneal** (the integrative session is where the contract gets its public
form — the anneal is the same act):
- Durable files (`models.py`, `_pipeline_io.py`, `_pipeline.py`, `_tags.py`, `_audit.py` docstrings/comments)
  carry **no plan coordinates** — no "S1/S2/S3", no "sidecar-case-ids sub-track", no `/plan-run` vocabulary.
  State the property/reason/invariant (e.g. "applied contested-default case-IDs; set-union append-only per
  C-CASE-PROV"), never the plan coordinate.
- Grep the durable files against the **anneal denylist** (Notes for executors); translate any leaked coordinate
  into standalone prose.
- Report to the styleguide roadmap: rule-5.5 sidecar persistence is enacted; C-CASE-PROV frozen.  **No R6d
  coordination needed** — sidecar-only, no persisted-tag/path change.

## Cross-session contracts

### C-CASE-PROV — applied contested-default case-IDs in the provenance sidecar *(field frozen at S1; source set to be frozen at S2)*

**Field + persistence (frozen at S1).**  `ProvenanceSidecar.applied_case_ids: list[str]` (default `[]`) records
the register case-IDs (`<LAYER>-<n>`, per C-CASE) of the contested-case (P2) neutral defaults that were applied
for the release.  Persisted in the work-dir provenance sidecar (`freedb_disc_*.yaml` or
`music_annotator_provenance.yaml`), under the same C-PROV write discipline as `annotation_tier` (written once
per work dir, after `_verify_copy`, before the journal append).  **Merge semantics: set-union, append-only** —
`_write_provenance_fields` unions the incoming set with the recorded set and writes the **sorted** union; an
incoming empty list never shrinks or erases the recorded set.  This is deliberately *not* the monotonic-rank
rule of `annotation_tier` (a set grows; it has no rank).  Serialization is order-normalised (sorted) for
byte-stable re-writes.  Free-text is never written to tags (5.5): the case-IDs live only in the sidecar.

**Source set (to be frozen at S2).**  Which register cases are "applied" and whether each is run-derived (a
decision site fired for this release) or structural (always applied for a classical release) is the S2 juncture
judgment.  Candidate contested-default population: SEL-11, NORM-1, NORM-2, REND-1, REND-2, REND-14 (the P2 /
"contested by nature" register cases with identifiable application sites).  *This subsection over-specifies the
source set only after the S2 ruling; until then it is "to be frozen at S2".*

**Flavour:** compiler-enforced (the Pydantic field type; mypy strict on the merge arm) **+ test-enforced** (the
S1 set-union KATs: union grows, empty-never-erases, round-trip; the S2 behavioural KATs: right case-IDs per
release shape) **+ prose-enforced** (the source-set ruling, cited to 5.5/P2 and the register).  **Defined-in:**
S1 (field + merge) / S2 (source set).  **Consumed-by:** S2 (writes the field), S3 (reads it in audit), any
future sidecar reader / Act III-b re-derivation (the case-IDs are durable provenance).  Over-specified per
Category-A: carries the set-union-vs-monotonic asymmetry and the empty-never-erases guarantee even though only
S1's tests immediately exercise them.

### Consumed (frozen upstream — invalidation is out of scope for this sub-track)

- **C-TIER / C-AR** (R2 / R3b) — the sibling `ProvenanceSidecar` fields and their monotonic-upgrade merge arms;
  the new arm sits beside them without disturbing them.  Validate-only.
- **C-PROV** (repo `AGENTS.md`, transaction-journal/confirmation-provenance invariant) — the once-per-work-dir,
  post-`_verify_copy`, pre-journal-append write ordering.  `applied_case_ids` is written at the *same* gated
  site as `annotation_tier`; S2 must not append it before `_verify_copy` succeeds.
- **C-CASE** (styleguide arc) — register case-IDs are append-only, never renumbered; emitted strings are the
  survivor IDs after any consolidation.
- **STYLEGUIDE v1 rule 5.5 / P2 / layer 5** — the authority; no ruling is re-opened.

### Produced

- **C-CASE-PROV** — field + merge at S1, source set at S2.  Sidecar-only; **no output to R6d planning** (no
  persisted-tag or path change).

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Add applied_case_ids field and set-union merge to ProvenanceSidecar | pending | — | — |
| 2 | Source and thread applied contested-default case-IDs into the sidecar | pending | — | — |
| 3 ◆ | Surface applied case-IDs in the audit tier pass | pending | — | — |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **D-1 (S2 source-set judgment — the run-derived vs structural line).**  The contested-default population and
  the per-case run-derived/structural classification is the S2 juncture judgment; a wrong set is a
  *prose*-contract error, not a compiler one.  If S2 finds a candidate case (SEL-11/NORM-1/NORM-2/REND-1/2/14)
  has **no clean application site** — e.g. the default is applied implicitly with no branch to instrument —
  that is an **additive-reshard** signal (the source set is narrower than the survey implied); surface it, do
  not manufacture a synthetic site.
- **D-2 (composite-tag-grammar shard provisionally discharged).**  The substrate survey found ARTIST/ALBUMARTIST
  already render verbatim MB credits with no author-splicing (REND-1/4.3 satisfied) and CEA composites already
  correctly separated (C-RA-GRAMMAR) — no un-enacted grammar work without an operator-named target.  Folded to
  ROADMAP-styleguide D-A6.  Not a risk to *this* sub-track; recorded so a future node-A shard does not re-derive
  it.  **internal-continue** (does not affect the sidecar-case-ids sessions).
- **D-3 (normalisation shard carries an unresolved conflict).**  STYLEGUIDE 3.1 (compact projections render
  canonical) vs. REND-1/4.3 (`ARTIST` preserved/verbatim) conflict, plus a library-wide repath requiring R6d
  coordination — needs adjudication before that shard is shardable.  Recorded for the next node-A boundary; **no
  bearing on this sub-track** (sidecar-only).
- **D-4 (no R6d coupling — sequencing freedom).**  Unlike the other node-A shards, this one changes no persisted
  tag and no path, so it is **independent of R6d** and can land any time without the "re-derive once" pressure.
  No dependency inversion; safe to land now.
- **D-5 (stale census/NOTES `cea_album_soloists_unified` refs — pre-existing, out of scope).**  Carried down
  from the A-shards ◆ deferral and both roadmaps' R6d caveat: `census-impl.md` / `NOTES.md` still describe the
  deleted field.  A doc-freshness item for R6d, **not** this sub-track's work (census-artifact content refresh).
  Noted so `/plan-run` does not treat it as an in-track discovery.

## Notes for executors

- **Tier routing.**  S1, S2 are **Opus** (durable contract freeze; the sourcing-model judgment).  S3 is
  **Sonnet** (mechanical audit-surface mirror of `_audit_tier_pass`).  `juncture-tier: opus` — kept because
  C-CASE-PROV is durable sidecar-persisted state and S2's source set is a judgment tests cannot fully catch,
  despite the sidecar-only / no-R6d posture.
- **Register: application, not authoring.**  No new editorial decisions.  Every applied case-ID is an
  *already-frozen* v1 ruling; S2 records which bit, it does not decide.  If a row seems to *need* a new
  contested-case ruling, that is a discovery (surface it), not a licence to decide.
- **REGISTER rule (durable-file discipline).**  In source/tests, state the *property/reason/invariant* — never
  the plan coordinate.  "applied contested-default case-IDs; set-union append-only" is right; "the S2
  case-ID threading" is not.  Plan vocabulary (S1/S2/S3, sub-track names, `/plan-run`) lives only in
  `PLAN.md` / `ROADMAP*.md` / the ledger / commit messages.  See also the repo `AGENTS.md` REGISTER block.
- **Anneal denylist (◆ gate greps durable files for these).**  Seeded from the `/plan-run` default, tuned for
  this project's vocabulary:
  - `\bS[1-9]\b` (plan session coordinates) — **but** allow the legitimate STYLEGUIDE-rule-section forms
    (`\b[45]\.[0-9]\b` like "4.5", "5.5" are register/rule cites, not plan coordinates — do **not** flag).
  - `sub-track`, `plan-run`, `plan-shard`, `halt-at-boundaries`, `run-to-boundary`
  - `C-CASE-PROV` **only outside docstrings that legitimately name the contract** — contract names in
    docstrings are the intended durable form (the C-TIER/C-AR precedent); flag bare "S2 freeze"-style prose,
    not the contract name itself.
  - `juncture`, `inflection`, `action-frame`, `◆`
  - Do **not** add `case-ID` / `applied_case_ids` / register IDs (`SEL-`, `NORM-`, `REND-`) to the denylist —
    these are legitimate domain vocabulary this sub-track deliberately persists.
- **Invariants to preserve:** C-PROV write ordering (case-IDs written at the same gated site as
  `annotation_tier`, after `_verify_copy`, before journal append); C-TIER/C-AR merge arms untouched; C-CASE
  append-only (emit survivor IDs); nothing free-text in tags (5.5 — case-IDs live only in the sidecar); the
  defensive-download and confirmation-provenance invariants (untouched — this sub-track is not in the
  copy/verify network path).
- **Every row runs `~/.local/bin/tox -m analyze` before ledger-done** (build + test at 100% branch coverage +
  strict mypy + ruff + pylint 10.00/10 + pyupgrade).  Import order via `~/.local/bin/tox -m edit`, never
  hand-edited.
- **Suggested first `/plan-run` invocation:** `halt-at-boundaries` — this is the first sidecar-provenance
  application shard from the styleguide arc; the "persist an applied-ruling set" pattern is unproven here, so
  stop after S1 for an operator check that the C-CASE-PROV freeze (especially the set-union-vs-monotonic
  merge semantics) is right before S2 consumes it.  Once S1 confirms the pattern, `run-to-boundary` through the
  S3 ◆.
