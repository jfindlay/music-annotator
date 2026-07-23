<!-- juncture-tier: sonnet -->
<!-- sub-track: V1a (source mining) — styleguide arc (docs/ROADMAP-styleguide.md); the three-source evidence reservoir V1b adjudicates from; first sub-track after E0 seed; three mutually-independent Category-B mining sessions; juncture-tier opted DOWN to sonnet (lever 3+4 low: census misclassifications are cheap and caught downstream when V1b consumes them — the V1b-as-inner-loop lever-5 analog) -->

# PLAN — V1a: source mining (the three-source evidence reservoir)

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Build the **three census artifacts** V1b authors STYLEGUIDE v1 from, so that every v1 ruling is grounded
in all three founding sources rather than in one source's biased view.  The styleguide's v1 posture
(ROADMAP-styleguide "V1 posture"): what v1 freezes is the *architecture* (five layers, two partitions,
case-ID scheme — already frozen at E0) and the *adjudication method* (rulings grounded in three-source
evidence, citing principles).  V1a produces the evidence; it makes **no rulings** and authors **no
STYLEGUIDE content** — that is V1b's exclusive register.

**Why three sources, and why they must be mined separately (the triangulation — ROADMAP-styleguide
lines 30–36).**  Each source has a solo failure mode the other two correct:

- **CE docs** — the *intended* stance.  Every CE option is a documented editorial fork; a pre-compiled
  hard-case census.  Solo failure: options without frequencies, no rulings.
- **The implementation** — the *enacted* stance.  De-facto adjudications in `src/`, some deliberate,
  some accidental.  Solo failure: accidents mistaken for intentions.
- **The library data** — the *empirical* stance.  Case frequencies, concrete instances, cross-release
  variance proving cases editorial.  Solo failure: a biased single-collector sample.

Rulings grounded in all three are what make v1 impressive **without pretending completeness**.  The
three sessions are therefore mutually independent (parallelizable) — no census consumes another; they
converge only at J-E1, where their *joint* sufficiency for adjudication is judged.

**The extraction rubric (identical for all three — ROADMAP-styleguide lines 71–74).**  Every mining
session: (1) classify every finding onto the **five-layer schema** (Ontology / Selection /
Normalisation / Rendering / Epistemic — STYLEGUIDE "Architecture"); (2) map onto **existing case-IDs**
where a finding fits one (SEL-1..11, NORM-1..2, REND-1 — STYLEGUIDE "Case register"); (3) **mint new
cases** (append-only, per C-CASE) where none fits; (4) record evidence with enough provenance that V1b
can adjudicate **without re-mining**.  Census artifacts are evidence reservoirs consumed by V1b —
**STYLEGUIDE.md never cites them** (they are internal working documents, not the human deliverable).

**The anti-defocus line for V1a specifically.**  A large volume of newly-minted cases is a *signal to
surface at J-E1*, not to absorb — it means the E0 seed taxonomy may have mis-shaped the space
(ROADMAP-styleguide "J-E1").  Mining sessions mint freely into census artifacts but **never renumber or
edit the E0 register** (C-CASE); absorption into the register happens at V1b.  Any discovered conflict
with a frozen library-completion-arc contract (C-CLASS / C-INIT) is **flagged to that arc's boundary,
never re-opened in-arc** (ROADMAP-styleguide "Cross-arc coupling").

## Verify gate

**V1a touches no gated code.**  S1/S2 write only markdown census artifacts under `docs/`; S3 adds a
read-only scanner under `scripts/`, which is **outside the packaged tree** (`pyproject.toml`
`packages = ["music_annotator"]`) exactly as the R0 precedent `scripts/census_original.py` is.  So the
`src/`+`tests/` gate has **no V1a session to fail it**.  The gate is stated for documentation and for
the one place it *could* bind (see S3):

- **VERIFY_TEST**: `~/.local/bin/tox -e test` — pytest, 100% branch coverage (`fail_under = 100`).  No
  V1a artifact is inside this scope; the S3 scanner lives in `scripts/` (untested by house convention,
  matching `census_original.py` / `scan_nonuniform_depth.py`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` — mypy strict on `src/ tests/`.  The S3 scanner is
  outside `src/`; if the executor chooses to type-check it, run mypy against the script path explicitly
  (not required by the gate).
- Full gate (`~/.local/bin/tox -m analyze`) is **not a V1a boundary condition** — no ◆ here changes
  gated code.  The J-E1 juncture (after S3) is an *evidence-sufficiency* review, not a green-gate.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 | Mine CE documentation into the editorial-fork inventory | B | Sonnet | **C-CASE**, five-layer schema + case register (STYLEGUIDE), CE-continuity posture | `docs/census-ce.md` |
| 2 | Mine the implementation into the de-facto rulings inventory | B | Sonnet | **C-CASE**, five-layer schema + case register (STYLEGUIDE), frozen C-CLASS/C-INIT (validate-only) | `docs/census-impl.md` |
| 3 ◆ | Mine the library into the empirical census (+ read-only scanner) | B | Sonnet | **C-CASE**, five-layer schema + case register (STYLEGUIDE), census-r0 scanner precedent | `docs/census-library.md`, `scripts/census_styleguide.py` |

`Cat`: all three **B (algorithm)** — each mines one self-contained source against the fixed extraction
rubric; mutually orthogonal, no cross-session contract beyond the shared C-CASE + schema (the substrate
was frozen at E0, so V1a has no Category-A substrate session of its own).
`Tier`: **Sonnet on all three** — the judgment surface is constrained by the E0-frozen schema and case
register; mining is rubric-bounded classification, not interface design.  No `@architect` session in
V1a.  The one high-judgment point in this sub-track — is the census *jointly* sufficient for
adjudication? — is the **J-E1 juncture** *after* S3, adjudicated by the (sonnet) juncture fork, not
in-session.
`◆` on **S3** — sub-track-final; its boundary closes V1a and hands off to **J-E1** (mining→authoring
juncture) en route to V1b.  No ◆ on S1/S2 (mid-sub-track; the three censuses are not jointly complete
until S3).  No `@architect` marker on any row (register is Sonnet-autonomous per ROADMAP-styleguide
line 70; V1b is where the architect-on-Fable register returns).

**Split/merge rationale (levers named).**  Session count is **3**, matching the roadmap's V1a estimate
exactly — no split or merge.  The **one-line-commit-title corollary** passes cleanly: "Mine CE
documentation", "Mine the implementation", "Mine the library" are three distinct commit-shaped titles,
one per source.  The boundaries are **source-sharp** (each session mines exactly one of the three
triangulation sources — the legitimate boundary, not a fractured floor): the sources are epistemically
distinct (intended / enacted / empirical) and their solo failure modes are corrected only by keeping
them separate.  **Lever 2 (the floor)** holds each session at *one-source-whole* — a partial census of
one source would force V1b to re-mine, the exact coupling the artifact exists to prevent — so no session
is fractured below its irreducible unit.  **S3 legitimately carries slightly more** (census + the
scanner that produces it): the empirical census *requires* the tool, so census+scanner is one
conceptual unit, not a merge of two (the R0 precedent bundled `census_original.py` with `census-r0.md`
identically).  **Levers 3 (low design-error cost) and 4 (low criticality)** keep the sessions small and
would license further splitting, but lever 2's one-source-whole floor is the binding constraint here.

## Session detail

### S1 — Mine CE documentation into the editorial-fork inventory → `docs/census-ce.md`

**Deliverable.**  The **editorial-fork inventory**: every Classical Extras option and default recorded
as a documented editorial fork, classified by the five-layer schema, plus CE's tag vocabulary and
semantics (the compatibility floor, enumerated) and CE's ordering/grammar conventions.  Sources
(ROADMAP-styleguide lines 76–80): the Classical Extras plugin documentation and README (picard-plugins
2.0 tree) and its user guide; supplementary where CE cites them — MusicBrainz classical style
guidelines, Picard community classical naming scripts.

**≥1 KAT (census-artifact form).**  A census artifact's "KAT" is a **coverage assertion the artifact
makes about itself**, checkable by V1b without re-mining: *every CE configuration option in the plugin's
options UI appears as exactly one classified fork row, and every row carries (layer, mapped-or-minted
case-ID, evidence citation)*.  State this completeness claim explicitly at the head of `census-ce.md` so
J-E1 can verify it.  (A mining session whose deliverable can't assert a coverage KAT has an undefined
contract — this one can: "all CE options enumerated and classified".)

**Subtleties.**
- **CE docs are the *intended* stance** — a CE option's *existence* is the evidence (each option is a
  documented editorial fork), even where CE ships a default.  Record both the fork and CE's default
  choice; do not collapse "CE picks X by default" into "the only option is X".
- **CE-continuity posture (STYLEGUIDE standing rules).**  CE tag semantics are the compatibility floor;
  enumerate shared tag names with their CE meanings so V1b can honour "extensions are additive, never a
  redefinition".  Flag any CE convention the *implementation* (S2) may have diverged from — but do not
  adjudicate the divergence (that is V1b).
- **Mint into the census, never the register.**  A CE option with no matching E0 case gets a minted
  case-ID in `census-ce.md` per C-CASE (append-only `<LAYER>-<n>`); the E0 STYLEGUIDE register is not
  edited.

**Deferrals.**  No rulings, no STYLEGUIDE edits (V1b).  No cross-referencing against the other two
censuses (they don't exist yet and the sessions are independent — cross-source reconciliation is J-E1 /
V1b work).

### S2 — Mine the implementation into the de-facto rulings inventory → `docs/census-impl.md`

**Deliverable.**  The **de-facto rulings inventory**: every editorial choice *enacted in `src/`*, each
classified deliberate-vs-accidental with a **ratify/overturn queue for V1b**.  The roadmap enumerates
the targets (ROADMAP-styleguide lines 81–84): role-classification heuristics, credit orderings,
separators, composite-tag sources, path-grammar components, the concerto path-injection
(`_tags.py:1189`, gated on `top_work.type == "Concerto"`), and the frozen C-CLASS / C-INIT shapes.
Classify each onto the five-layer schema and map/mint case-IDs.

**≥1 KAT (census-artifact form).**  Coverage assertion: *every editorial choice site named in the
roadmap target list is inventoried with (layer, case-ID, deliberate|accidental verdict, source
location, ratify/overturn recommendation)*, and the enacted `ARTIST` grammar
(`_pipeline.py:1742`, "MB recording credit verbatim") is captured as the REND-1 evidence.  State the
target-coverage claim at the artifact head for J-E1.

**Subtleties.**
- **Deliberate-vs-accidental is the census's core discrimination, not a ruling.**  Marking a choice
  "accidental" is an *observation about the code's intent* (e.g. a separator that falls out of a join
  with no editorial decision behind it), queued for V1b to ratify or overturn — it is **not** a ruling
  that it is wrong.  Keep the verdict evidential.
- **C-CLASS / C-INIT are validate-only (frozen upstream — ROADMAP-styleguide "Cross-arc coupling").**
  Record the enacted class-routing and within-classical shapes as evidence; if mining surfaces an
  *apparent* need to change either, that is a **finding for the library-completion arc's boundary**, not
  an in-arc re-open — log it in the census's discoveries section and surface at J-E1, never edit the
  contract.
- **The `_tags.py:1189` concerto gate is the seed of SEL-11** (canonical-soloist promotion; NOTES
  "Concerto-soloist path promotion" calls it a coherence violation in miniature).  Capture it as SEL-11
  evidence with the coherence-violation observation, but do **not** design its replacement (V1b / a
  post-v1 A-shard).
- Register the composite-`ARTIST` grammar as REND-1 evidence (STYLEGUIDE REND-1: composer-in-`ARTIST`).

**Deferrals.**  No `src/` edits — S2 is read-only mining (the whole of V1a is code-out-of-scope save the
S3 `scripts/` scanner, ROADMAP-styleguide "Out of scope").  No ruling on any ratify/overturn queue
entry (V1b).  No C-CLASS/C-INIT change.

### S3 ◆ — Mine the library into the empirical census (+ read-only scanner) → `docs/census-library.md` + `scripts/census_styleguide.py`

**Deliverable.**  The **empirical census**: per-case frequency estimates and concrete instances proving
cases editorial, produced by a **read-only scanner** in `scripts/` (the `census_original.py` precedent).
The roadmap names the target measurements (ROADMAP-styleguide lines 85–90): multi-soloist releases,
conductor-less ensembles, choir+orchestra, completer/arranger credits, play-direct, opera principal
counts; **attribution-variance instances** (same work, different credit sets across releases — the
proof that selection is editorial); **name-form variance** (same artist MBID, different rendered forms —
the normalisation/fragmentation evidence).  Classify each measurement onto the schema and map to
case-IDs (SEL-*/NORM-* especially).

**≥1 KAT (census-artifact form).**  Coverage assertion: *every SEL-* and NORM-* case in the E0 register
carries either a frequency estimate + ≥1 concrete instance, or an explicit "not observed in this
library" note* (an honest empty is evidence too — P3 failure-vs-no-data).  The scanner itself gets a
minimal smoke check only if the executor elects it (untested-by-convention like `census_original.py`);
the artifact-level coverage claim is the binding KAT.

**Subtleties.**
- **Host-path caveat (ROADMAP-styleguide line 89–90; NOTES "Note on host paths").**  Run against the
  **canonical library root** (or a matching mount).  A mismatched `dest_root` is a **silent no-op
  hazard** — the scanner must fail loudly (not produce an empty census) when the library root is absent
  or empty.  Reuse the `census_original.py` relative-path join discipline (absolute-path joins are the
  documented silent-no-op hazard).
- **The library is a biased single-collector sample (its solo failure mode).**  Frequencies are
  *estimates from one collection*, not population statistics — label them so; V1b must not over-weight a
  frequency of 1.  Cross-release *variance* (the same work credited differently) is the durable
  evidence; raw counts are context.
- **Scanner scope: read-only, `scripts/`-resident, ungated.**  No `src/` change, no gate impact
  (`packages = ["music_annotator"]` excludes `scripts/`).  Model it on `census_original.py`'s Pass-1
  offline evidence sweep (mutagen tag probe, shape stats) — the styleguide census needs the *attribution
  fields* (performer/role tags, credit strings, MBIDs), not the provenance axis.
- **Mid-library reality (Discoveries D-2).**  The library is a *mix* of already-ingested (`Done/`) and
  not-yet-ingested (`Original/`) trees, and of two-level (pre-R4a) and three-level (post-C-CLASS)
  paths.  The scanner should census the **annotated** material (where credit/role tags exist) — decide
  and document which root(s) it walks; an incomplete-library caveat belongs in the artifact.

**Deferrals.**  No rulings (V1b).  No MB network calls required (this is an *empirical* census of the
*local* library's enacted credits; MB-lookup-driven analysis is out of scope — contrast R0's Pass-2).
No absorption of minted cases into the register (V1b).

## Cross-session contracts

### C-CASE — case-ID stability *(FROZEN at V1a start — i.e. here)*

Case-IDs are `<LAYER>-<n>` with `<LAYER>` in {`ONT-`, `SEL-`, `NORM-`, `REND-`, `EPIST-`};
**append-only, never renumbered or reused**.  Minting happens **in census artifacts** during V1a and is
absorbed into the STYLEGUIDE register at V1b.  Load-bearing because provenance sidecars will persist
these IDs (STYLEGUIDE rule 5.5) — a renumber would orphan persisted marks.  **Flavour: prose-enforced.**
**Defined-in:** E0 (STYLEGUIDE "Case register"), frozen for the arc at V1a start.  **Consumed-by:** all
three V1a sessions (each mints under it) and V1b (absorbs).  *Nothing in V1a edits the E0 register; all
mints live in census artifacts until V1b.*

### Consumed (frozen upstream — invalidation is out-of-scope for V1a)

- **The STYLEGUIDE architecture** (five layers, two partitions, case-ID scheme — E0) and the epistemic
  rules 5.1–5.5 (E0): the fixed classification schema every mining session applies.  **Flavour:
  prose-enforced.**  An apparent need to change the *schema itself* (e.g. a finding that fits no layer)
  is a **J-E1 surface** (the seed taxonomy mis-shaped the space), never an in-session schema edit.
- **C-CLASS / C-INIT** (library-completion arc; `_top_level_class(tags)`, `_classical_top_dir(tags)`):
  consumed **validate-only** by S2.  An apparent conflict is a **finding for the library-completion
  arc's boundary** (ROADMAP-styleguide "Cross-arc coupling"), logged and surfaced at J-E1 — never an
  in-arc re-freeze.  **Flavour: compiler+test-enforced (in the other arc; here it is a validate-only
  boundary).**
- **The CE-continuity posture** (STYLEGUIDE standing rules): CE tag semantics are the compatibility
  floor; S1 enumerates them so V1b can keep extensions additive.  **Flavour: prose-enforced.**

### Produced

- **C-CASE** is frozen here (its subsection above is fully resolved — a prose contract with no substrate
  to discover, so no *"to be frozen at …"* deferral).  V1a produces **no other contract** — the census
  artifacts are evidence, not interfaces.  **C-ONT** (layer-1 taxonomy) is produced downstream at
  V1b-S4, out of V1a scope.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Mine CE documentation into the editorial-fork inventory | done | dcf55f2 | C-CASE (consumed; 35 cases minted into census, not register) |
| 2 | Mine the implementation into the de-facto rulings inventory | done | 45681bd | C-CASE (consumed; 24 cases minted into census, not register) |
| 3 | Mine the library into the empirical census (+ read-only scanner) | done | 58f7f9b | C-CASE (consumed; 3 cases minted into census, not register) |

## Action-frame digest

### S3 ◆ — 2026-07-22 (J-E1 boundary-transform)
Discovery/flex: 62 new cases minted across V1a (vs E0's 14); boundary-transform fork returned still-on-intent.
Affected: C-CASE (consumed; mint volume is a D-1 J-E1 signal, not a taxonomy mis-shaping — no D-4 triggered).
Deferred: yes — two operator recommendations for V1b: (1) REND- merge-assessment (26 cases, REND-14/15/16 and REND-17/18 are consolidation candidates); (2) run scanner on hades for authoritative frequencies before V1b adjudication.
Texture: library census produced from available evidence (library not accessible in dev); honest labeling sufficient for V1b adjudication per fork verdict.

## Discoveries & risks

Phrased as `/plan-run` reads for discovery adjudication (internal-continue / additive-reshard /
destructive-HALT).  Carried down from the ROADMAP-styleguide "Design intent", "J-E1", and "Cross-arc
coupling" sections.

- **D-1 (a large case-mint is a J-E1 signal, not an in-session absorption).**  The extraction rubric
  mints freely, but a *large volume or unexpected shape* of newly-minted cases means the E0 seed
  taxonomy may have mis-shaped the space (ROADMAP-styleguide "J-E1").  Mint into the census;
  **surface the volume at the ◆ / J-E1** — do not renumber or restructure the E0 register
  (**internal-continue** for minting; the volume itself is an **additive-reshard** signal for J-E1 to
  weigh, e.g. whether V1b's 3-session split still fits the evidence).
- **D-2 (the library is a biased, mixed-state single sample).**  Frequencies are one-collection
  estimates (not population stats); the library mixes ingested/not-ingested and two-/three-level trees.
  S3 must label estimates as such and document which root(s) it walks.  **Internal-continue** — this is
  a known property of the sample, not a defect; over-claiming statistical significance would be defocus.
- **D-3 (C-CLASS/C-INIT conflict = other-arc finding, never in-arc re-open).**  If S2 surfaces an
  apparent need to change the frozen class-routing/within-classical contracts, that is a finding for the
  **library-completion arc's** boundary (its `docs/ROADMAP.md`), logged in the census and surfaced at
  J-E1.  Attempting to edit C-CLASS/C-INIT from V1a is **destructive-HALT** (cross-arc frozen-contract
  invalidation).
- **D-4 (schema-fit failure = J-E1 surface, not schema edit).**  A finding that fits no layer of the
  five-layer schema is evidence the seed architecture is incomplete — surface it at J-E1 (the same
  place a large mint surfaces).  Editing the STYLEGUIDE architecture from a mining session is
  **destructive-HALT** (E0-frozen substrate invalidation).
- **D-5 (V1a makes no rulings and authors no STYLEGUIDE content).**  The register boundary is absolute:
  mining classifies and mints into census artifacts; adjudication and STYLEGUIDE authoring are V1b's
  exclusive register.  A session that begins ruling on a case (rather than recording it as evidence) has
  drifted into V1b — **internal-continue** by re-scoping to evidence; if the executor cannot avoid
  ruling to complete the census, that is a **reshard** signal (the session boundary was wrong).
- **D-6 (host-path silent-no-op hazard — S3).**  A mismatched/absent library root must make the S3
  scanner **fail loudly**, never emit an empty census (which would silently under-report every
  frequency).  Reuse `census_original.py`'s relative-path-join discipline.  A scanner that emits an
  empty census without erroring is a correctness defect to fix in-session (**internal-continue**).

## Notes for executors

- **Tier routing.**  All three sessions **Sonnet-autonomous** (ROADMAP-styleguide line 70) — mining is
  rubric-bounded classification against the E0-frozen schema, not interface design.  **No `@architect`
  session in V1a.**  The sub-track's one high-judgment point is **J-E1** (evidence-sufficiency
  adjudication after S3), and its `juncture-tier` is **sonnet** (header) — opted down because census
  misclassifications are low-cost and caught downstream when V1b consumes them (the V1b-as-inner-loop
  lever-5 analog; ROADMAP-styleguide lines 13–16).
- **Register: mining, not authoring.**  Census artifacts are internal evidence documents (not the
  human-facing STYLEGUIDE, not agent rolling-context).  Prose can be working-register (tables, terse
  evidence rows) — they exist to be *consumed by V1b*, not published.  **STYLEGUIDE.md is never cited by
  a census and is never edited in V1a.**
- **Invariants to preserve (do not regress):** C-CASE append-only case-IDs (mint in census, never touch
  the E0 register); the five-layer schema as the fixed classification target; C-CLASS/C-INIT
  validate-only (cross-arc frozen); the extraction rubric's "record enough provenance that V1b needn't
  re-mine"; P3 failure-vs-no-data (an honest "not observed" is evidence, an empty census from a bad path
  is a defect).
- **Independence.**  The three sessions are mutually independent (parallelizable) — no census reads
  another.  Cross-source reconciliation is **J-E1 / V1b** work, not a mining session's job.  S3 alone
  touches `scripts/` (no `src/`, no gate impact).
- **Sequencing.**  V1a is the first sub-track after E0 seed, on the styleguide arc's critical path
  **V1a → J-E1 → V1b → v1**, whose v1 gates the library-completion arc's **J2** (naming-policy freeze).
  On the S3 ◆, V1a closes and hands to **J-E1** (mining→authoring juncture: census quality sufficient?
  mint volume/shape?  does V1b's 3-session split still fit?), then **V1b** (interactive, operator-
  adjudicated authoring — architect-on-Fable register returns there).
- **Suggested `/plan-run` invocation:** `/plan-run halt-at-boundaries` — this is an **unproven shard
  pattern** (first styleguide-arc shard; census-artifact "KATs" are coverage assertions, not code
  tests, so the inner-loop green-gate does not apply and the ◆/J-E1 evidence-sufficiency review is the
  real checkpoint).  A boundary halt lets the three censuses' *joint* sufficiency be adjudicated at
  J-E1 before V1b consumes them — which is exactly what J-E1 exists to decide.
