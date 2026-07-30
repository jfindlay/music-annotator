<!-- juncture-tier: n/a — interactive arc: no automated junctures fire (ROADMAP-styleguide lines 15–16); every boundary
     judgment is made live with the operator.  This PLAN is an operator-run agenda + contract tracker, NOT a /plan-run
     target. -->
<!-- sub-track: V1b (authoring) — styleguide arc (docs/ROADMAP-styleguide.md); consumes the three V1a censuses;
     interactive, operator-adjudicated, architect-on-Fable register; produces STYLEGUIDE v1, which satisfies the
     library-completion arc's J2 gate. -->

# PLAN — V1b: authoring (three-source adjudication → STYLEGUIDE v1)

## Purpose (design intent)

*(Re-read at every session start — anti-defocus anchor.)*

Author STYLEGUIDE v1 by adjudicating the case register against the three V1a censuses (`census-ce.md` — intended;
`census-impl.md` — enacted; `census-library.md` — empirical).  The v1 posture (ROADMAP-styleguide "V1 posture"): what v1
freezes is the *architecture* (already frozen at E0) and the *adjudication method* (rulings grounded in three-source
evidence, citing principles); individual rulings carry status and remain revisable through the post-v1 L loop.  v1 need
not be complete or excellent — it is designed knowing it will be improved.  **The operator is the editorial authority**:
sessions are interactive; the agent drafts, structures, and argues; the operator rules.

The register boundary inverts from V1a: mining is over, authoring begins.  Census artifacts are consumed and **never
cited by STYLEGUIDE.md** (universality guarantee — the styleguide is a self-contained human document; rulings cite
principles, not working documents).  Absorption of the 62 V1a-minted cases into the register happens here, per-layer, as
each session rules on them (C-CASE: append-only; merges are cross-referencing adjudications, never renumbers).

**Standing operator adjudication (2026-07-23), binding on all three sessions:** the concerto:soloist hack is dropped.
SEL-11 (canonical-soloist promotion) is **overturned** — a concerto release always carries the soloist in its tags;
nothing is promoted into the path grammar.  REND-16 (concerto path soloist-first ordering) is moot with it.  S4 records
the ruling; S6's path grammar omits the injection; the `_tags.py:1189` code removal is a post-v1 A-shard (trivial
deletion, coordinated with R6d).

## Verify gate

**V1b touches no code at all** — all three sessions write `docs/STYLEGUIDE.md` (+ NOTES adjudication log, + this
ledger).  The `src/`+`tests/` gate has no V1b session to fail it; stated for documentation only:

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (not binding — docs-only commits).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (not binding).
- The real per-session gate is each session's **coverage assertion** (Session detail) plus operator sign-off, and at
  the S6 ◆ the roadmap's "Done means V1b" checklist.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 4 | Adjudicate the sharp selection cases and author layer 1 (ontology) | A | @architect (Fable, live) | C-CASE, censuses ×3, SEL-11 pre-adjudication | `docs/STYLEGUIDE.md`, `docs/NOTES.md` |
| 5 | Adjudicate the remaining register and author layers 2–3 | B | @architect (Fable, live) | **C-ONT**, C-CASE, censuses ×3 | `docs/STYLEGUIDE.md`, `docs/NOTES.md` |
| 6 ◆ | Author layer 4 rendering grammars and the CE-divergence register; integrate → v1 | I | @architect (Fable, live) | **C-ONT**, all prior rulings, S2 ratify/overturn queue | `docs/STYLEGUIDE.md`, `docs/NOTES.md` |

`Cat`: S4 is **A (substrate)** — C-ONT (the layer-1 role taxonomy + canonical-identity definition) is the interface
every layer-2/3/4 rule and every post-v1 A-shard consumes; strongly serial, worth over-specifying.  S5 is **B** — bulk
adjudication against the frozen ontology.  S6 is **I (integrative)** — per the planning manual, consistently
under-scheduled; full session minimum, do not compress.
`Tier`: all three **operator-live @architect on Fable** (roadmap-frozen register).  No autonomous rows; no juncture
forks.  The ◆ on S6 closes the sub-track and reports **v1 to J2** (library-completion ROADMAP junctures table).

**Sizing (levers named).**  Three sessions, matching the roadmap estimate; J-E1 confirmed the split still fits the
evidence.  Sessions are sized by **operator-attention span**, not LOC (the deliverable is a document; the operator is
the inner loop — lever 5 is n/a in its test-suite form).  **Lever 2 (the floor)** shapes all three boundaries: S4's
ontology-emerges-from-the-sharp-cases is one irreducible unit (adjudicating SEL-1/2/6 and authoring the taxonomy are the
same act — NOTES session-1 close prescribed exactly this shape); S5's rulings+generalisation cohere because layers 2–3
are *generalised from* the accumulated rulings; S6's grammars+divergence-register+coherence-pass is the standard
integrative close.  **Known risk:** S5 carries ~35 rulings (all SEL except the four sharp ones, NORM-1..10, EPIST-1..8)
— if it overruns the operator's session, the pre-authorized split point is the layer-2/layer-3 boundary (contract-sharp:
selection rules freeze before normalisation rules generalise).  One-line-commit-title check: all three rows pass.

## Session detail

### S4 — Adjudicate the sharp selection cases and author layer 1 (ontology) — freezes C-ONT

**Deliverable.**  Layer 1 authored in STYLEGUIDE.md: the role taxonomy (soloists / ensembles / conductors spine + the
boundary roles the censuses surfaced) and the **canonical-identity definition** (what belongs to what a work *is* vs. to
a performance of it) — authored *from* the live adjudication of SEL-1 (ambiguous soloist), SEL-2 (concerto grosso),
SEL-6 (play-direct), not in the abstract.  SEL-11 recorded as **overturned** (operator pre-adjudication — the standing
decision above).  ONT-1..10 absorbed into the register with rulings or documented-open statuses.  Freezes **C-ONT**.

**Coverage assertion (the session's KAT-analog).**  Every ONT-* case absorbed and statused; SEL-1/2/6 adjudicated with
three-source evidence; SEL-11 statused overturned; layer 1 prose replaces its "(to be authored)" stub; the
canonical-identity definition is explicit enough that SEL-11's overturn and the compact projections both derive from it.

**Subtleties.**
- Over-specify the taxonomy (Category-A discipline): boundary roles the censuses prove real — chorusmaster (SEL-3),
  guest soloists (SEL-5), opera principals (SEL-7), completers/arrangers (SEL-8), vocal soloists in choral works
  (SEL-22) — should get taxonomy *positions* now even where their selection rules wait for S5; adding a position later
  is costlier than carrying one.
- The `"writer"`-merge divergence (recording-level merges into composers vs. work-level own bucket — SEL-17/18) is
  ontology-adjacent — position it in the taxonomy here, rule it in S5.

**Deferrals.**  No layer-2/3/4 prose.  No REND rulings.  No code.

### S5 — Adjudicate the remaining register and author layers 2–3

**Deliverable.**  Every remaining SEL-*, NORM-*, EPIST-* case ruled or documented-open (both are rulings; an unstatused
case is the defect).  Layers 2 (selection) and 3 (normalisation) core rules generalised *from* the accumulated rulings.
The census-impl ratify/overturn queue drained for its SEL/NORM/EPIST entries.

**Coverage assertion.**  Zero SEL/NORM/EPIST register cases without status; layers 2–3 stubs replaced; every
census-minted SEL/NORM/EPIST case absorbed.

**Subtleties.**
- Genuinely undecidable cases get a documented neutral default or documented-open status — generative neutrality is a
  founding principle, not a failure.
- Library frequencies are one-collection estimates from documentary evidence (D-2) — a frequency of 1 proves existence,
  not weight.

**Deferrals.**  REND rulings and all grammars (S6).  No code.

### S6 ◆ — Author layer 4 rendering grammars and the CE-divergence register; integrate → v1

**Deliverable.**  Layer 4 authored: per-surface grammars — `ARTIST` (REND-1 ruled) / `ALBUMARTIST`, the path-component
grammar (describing frozen C-CLASS/C-INIT, **without the concerto injection**), separators, orderings.  The **REND
merge-assessment** first: REND-14/15/16 (ordering family — note REND-16 moot) and REND-17/18 (separator family) are
consolidation candidates; merges are cross-referencing adjudications under C-CASE.  The **CE-divergence register**
written from the drained ratify/overturn queue.  End-to-end coherence pass; roadmap "Done means V1b" checklist;
**v1 ✓ — report to J2**.

**Coverage assertion.**  Zero REND cases without status; zero census-minted cases anywhere left unabsorbed;
CE-divergence register exists with rationale per divergence; layer-4 stub replaced; the tag-order (REND-14:
soloists-first) vs. path-order (REND-15: conductors-first) inversion is either ruled coherent (documented) or resolved.

**Subtleties.**
- Integrative sessions are consistently under-scheduled — full session minimum.
- The path grammar *describes* C-CLASS/C-INIT (frozen, other arc) — an apparent conflict is a library-arc boundary
  finding, never an in-arc change.
- Cross-surface coherence is founding principle 1: one attribution model, many projections — the grammars must derive
  from the S4 ontology, not restate the enacted code.

**Deferrals.**  All code (post-v1 A-shards: sidecar case-IDs, concerto-gate deletion, composite-tag and normalisation
changes — coordinate with R6d).  The public spec (P = R6e).

## Cross-session contracts

### C-ONT — layer-1 role taxonomy + canonical-identity definition *(to be frozen at S4)*

The substrate interface of the entire post-v1 space: every layer-2/3/4 rule, every A-shard, and CEv3 consume it.
**Flavour: prose-enforced.**  **Defined-in:** S4 (STYLEGUIDE layer 1).  **Consumed-by:** S5, S6, all post-v1 A/P/C
nodes.  Over-specified by design (Category-A discipline).

### SEL-11 pre-adjudication *(operator, 2026-07-23 — standing)*

Canonical-soloist promotion **overturned**; REND-16 moot; concerto path-injection dropped (code removal = post-v1
deletion shard).  **Flavour: prose-enforced.**  **Defined-in:** this PLAN + ROADMAP-styleguide Discoveries D-A3;
recorded in the register at S4.  **Consumed-by:** S4 (records), S6 (path grammar omits the injection), the A-shard
(deletes).

### C-CASE — case-ID stability *(frozen at V1a start; consumed here)*

Append-only, never renumbered/reused.  V1b performs the absorption: census mints move into the register per-layer as
ruled (S4: ONT; S5: SEL/NORM/EPIST; S6: REND).  Merges are cross-referencing adjudications ("adjudicated: consolidated
with REND-x"), never renumbers — sidecars will persist these IDs (rule 5.5).  **Flavour: prose-enforced.**

### Consumed (frozen upstream — invalidation is out-of-scope for V1b)

- **STYLEGUIDE architecture + epistemic rules 5.1–5.5** (E0): the fixed frame v1 fills.
- **C-CLASS / C-INIT** (library-completion arc): validate-only; S6 describes them in the path grammar; conflicts are
  that arc's boundary findings.
- **CE-continuity posture**: CE tag semantics are the compatibility floor; extensions additive; divergences documented
  with rationale (the S6 divergence register is this posture's enforcement artifact).
- **The three censuses** (V1a): evidence reservoirs; consumed, never cited by STYLEGUIDE.

### Produced

- **C-ONT** at S4 (above).  **v1** at S6 — the J2 gate input and the A/P/C substrate.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 4 | Adjudicate the sharp selection cases and author layer 1 (ontology) | done | 3a1d58f | C-ONT |
| 5 | Adjudicate the remaining register and author layers 2–3 | done | 38b1559 | — |
| 6 ◆ | Author layer 4 rendering grammars and the CE-divergence register; integrate → v1 | done | 386d092 | v1 |

## Action-frame digest

### S4 — 2026-07-23
Discovery/flex: The agent's draft soloist rule ("follow MB data") was rejected; the operator's replacement — soloist iff
reasonable confidence establishes a named/attributive solo, with work-format evidence, era sensitivity (Baroque and earlier
confer nothing by default), prominence-is-not-solohood, orchestral-principals-never, and ensemble-name precedence — is now
STYLEGUIDE 1.2 and the heart of C-ONT.  This *narrows* the soloist category relative to both CE and the enacted
implementation (both attribute every non-ensemble performer): S5 must re-examine SEL-4/5/7/22 and the ratify queue for
`build_cea_performers` against the narrowed category (the mechanical buckets remain valid as *credit* routing; they no
longer define solohood).
Affected: C-ONT (frozen this session, stronger than the pre-session sketch).
Deferred: yes — ONT-2 documented-open with direction (compositional containers canonical, editorial collections not; a
potential CE divergence for the S6 register).  ONT-11 minted (improvisational-primacy inversion — jazz boundary); scope
statement only, no rules.
Texture: SEL-11's overturn now *derives* from the 1.7 canonical-identity definition rather than standing as a bare operator
fiat — the register entry cites 1.7, and the jazz carve-out shows where the premise legitimately inverts.

### S5 — 2026-07-30
Discovery/flex: none structural — all 36 rulings landed within the S4 ontology, and the S4 carry-over (SEL-4/5/7/22 against
the narrowed soloist rule) resolved by citation rather than extension, validating C-ONT's strength.  SEL-8 ruled as the
mirror of SEL-11 (authors of the performed edition are canonical and enter compact projections; performers never are).
SEL-17 carries the binding buckets-are-credit-routing-not-solohood gloss (STYLEGUIDE 2.5) that the post-v1 composite-tag
A-shards consume.  Two true CE divergences recorded: SEL-13 (lyricist suppression overturned), NORM-6 (extended-title
splicing rejected).  D-S1-7 (EPIST schema-fit) closed by ruling: EPIST-1/4/5 adjudicated out-of-editorial-scope.
Affected: none frozen this session (C-ONT consumed, unmodified); layers 2–3 prose replaces the stubs.
Deferred: no split needed (D-5 risk did not materialise — the session fit).  Consolidations: SEL-21→SEL-2, NORM-10→NORM-2.
Texture: S6's queue grew two items — the chorusmaster-in-`CONDUCTOR` shared-tag narrowing (CE-continuity question for the
tag grammars) and the SEL-12→REND-1 handoff (the artist-slot fork is now wholly a layer-4 grammar question).

### S6 — 2026-07-30
Discovery/flex: the D-S2-1 "inversion" dissolved under adjudication — the path already renders billing order over its
occupied positions; the deviant surface was the tag assembly (REND-14), which the operator overturned in part by ruling
normalise-everything-to-billing-order.  A same-name-different-semantics hazard surfaced during that ruling: the enacted
composite recording-artist tag vs CE's verbatim-credit variable of the same name (remediation queued with the REND-14
shard).  Merges: REND-16→SEL-11, REND-18→REND-6, REND-8/9/11→REND-5; J-E1's REND-17+18 pairing rejected.
Affected: v1 produced (the J2 gate input).  No frozen contract touched; C-CLASS/C-INIT described, not defined.
Deferred: playlists' detailed grammar (4.6, to the L loop — honest gap); ONT-2/ONT-11 remain documented-open by design.
Texture: the 4.1 assembled-vs-preserved distinction did the session's structural work — it is why ARTIST/ALBUMARTIST
(preserved claims) and the editorial composites (assembled, billing order) resolve differently without incoherence.
Post-v1 A-shard queue out of S6: REND-14 reorder + naming realignment, chorusmaster-into-CONDUCTOR, IS_CLASSICAL
conditionalisation — all tag-shaping; land with R6d.

## Discoveries & risks

- **D-1 (REND merge-assessment — S6 opening move).**  26 REND cases; REND-14/15/16 and REND-17/18 are consolidation
  candidates (J-E1 operator recommendation).  Consolidation is adjudication with cross-references — renumbering is a
  C-CASE violation (**HALT-grade if attempted**; interactive, so the operator decides live).
- **D-2 (library evidence is documentary and waived-sufficient).**  census-library was produced without live library
  access; the operator reviewed and cleared V1b on it (2026-07-23).  Rule with existence-weight, not frequency-weight;
  the hades re-run feeds the L loop, not v1.  **Internal-continue.**
- **D-3 (C-CLASS/C-INIT are the other arc's frozen contracts).**  S6 describes, never redefines.  An apparent conflict
  is a finding for the library-completion arc's boundary.
- **D-4 (undecided ≠ unstatused).**  A neutral default or documented-open status is a ruling; a case with no status at
  v1 is the defect the S5/S6 coverage assertions exist to catch.
- **D-5 (S5 volume).**  ~35 rulings in one interactive session; pre-authorized split at the layer-2/layer-3 boundary if
  the operator's session overruns.  **Additive-reshard**, decided live.
- **D-6 (register-boundary inversion).**  V1b authors and rules; it does not re-mine.  A gap in the censuses is handled
  by ruling documented-open on present evidence (the L loop revises), not by re-opening mining — unless the operator
  elects a targeted spot-check.

## Notes for executors

- **This PLAN is not a /plan-run target.**  Suggested invocation: the operator opens one `@architect` (Fable) session
  per row, with this file as the agenda; the agent drafts and argues, the operator rules; commit per session (docs-only)
  and update the ledger row.  No juncture forks fire; the S6 ◆ review is performed live with the operator against the
  roadmap's "Done means V1b" checklist, then reported to J2.
- **Register: authoring, not mining.**  STYLEGUIDE.md is a self-contained human document — it never references
  NOTES/BACKLOG/ROADMAP/censuses.  Adjudication *records* (who ruled what, on which evidence) append to NOTES.md; the
  styleguide carries the rulings and their principled rationale.
- **Invariants to preserve:** C-CASE append-only (merges cross-reference); rendered-not-buried (rule 5.3); sidecar +
  case-IDs for contested marks (rule 5.5); CE-continuity (extensions additive, divergences documented); C-CLASS/C-INIT
  validate-only; the SEL-11 overturn (no concerto path-injection anywhere in v1's grammars); censuses never cited.
- **Sequencing.**  S4 → S5 → S6 strictly serial (C-ONT gates both successors).  On the S6 ◆: v1 ✓ → J2 (library-
  completion arc unblocks R6 planning); post-v1 A-shards become shardable, including the trivial concerto-gate deletion;
  the PLAN slot frees for the library arc's next sub-track (R4b or R5 drain).
