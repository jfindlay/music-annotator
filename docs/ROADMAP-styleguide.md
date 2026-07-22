# music-annotator — ROADMAP: editorial styleguide (node E arc)

The static-frame view of the styleguide arc: **V1 (mine three sources → author the initial guide)**, then the post-v1
application and externalisation nodes.  Durable for the arc's life; reviewed at sub-track boundaries.  Per-sub-track rolling
detail lives in `docs/PLAN.md` derived at each sub-track's start.  The styleguide *document* is `docs/STYLEGUIDE.md` — this
roadmap holds structure, not editorial content; the charter and session-1 adjudications live in `docs/NOTES.md` ("Editorial
attribution styleguide" + "Scope expansion").

This arc is a **peer of the library-completion arc** (`docs/ROADMAP.md`): its v1 gate feeds that arc's **J2**
(naming-policy freeze), and its post-v1 nodes outlive Act III-a.  Cross-references run in both directions; the styleguide
document itself references neither roadmap (human-doc register — the universality guarantee).

`juncture-tier: sonnet` — for the V1a mining chain only (user decision 2026-07-23): census inventories are
low correctness-criticality and every misclassification is caught downstream when V1b consumes them (the lever-5 analog —
the authoring sessions are the trustworthy inner loop for the mining sessions).  V1b sessions are interactive
(operator-adjudicated), so no automated junctures fire there; any V1b boundary judgment is made live with the operator.

## Design intent (anchor — re-read at every sub-track boundary)

The styleguide is the **universal editorial basis** for annotating recorded art music — implementation-independent,
realised by music-annotator standalone and eventually by CEv3 on Picard v3 (music-annotator will serve both roles: tagging
within and without Picard).  Three founding principles (STYLEGUIDE "Foundational principles"): cross-surface coherence
(one attribution model, many projections — composite tags such as `ARTIST` included), generative neutrality (neutral
defaults where editors legitimately differ; documented divergence), annotation-as-claim (the epistemic register).
CE-continuity posture: use and improve CE, never reconstruct its contract — CE/Picard tag semantics are the compatibility
floor, extensions are additive, divergences carry documented rationale.

**V1 posture (operator, 2026-07-23):** the initial guide need not be complete or excellent — it is designed knowing it
will be improved.  What v1 freezes is the *architecture* (five layers, two partitions, case-ID scheme) and the
*adjudication method* (rulings grounded in the three-source evidence, citing principles); individual rulings carry status
and remain revisable through the post-v1 loop.  The three sources triangulate: **CE docs** (the *intended* stance —
every CE option is a documented editorial fork; a pre-compiled hard-case census, but with no frequencies and no rulings),
**the music-annotator implementation** (the *enacted* stance — de-facto adjudications, some deliberate, some accidental;
each must be ratified or overturned), **the library data** (the *empirical* stance — case frequencies, concrete instances,
cross-release variance proving cases editorial; a biased single-collector sample alone).  Rulings grounded in all three
are what make a v1 impressive without pretending completeness.

**Done means:**

- **V1a** — three census artifacts exist (`docs/census-ce.md`, `docs/census-impl.md`, `docs/census-library.md`), each
  classified onto the five-layer schema, with new cases minted into the register under C-CASE.
- **V1b** — every register case has a ruling or a documented-open status; layers 1–4 carry their core rules; the
  CE-divergence register exists; STYLEGUIDE v1 is coherent end-to-end.  This satisfies the **J2 gate** in the
  library-completion ROADMAP.
- **Post-v1** — trigger- and operator-paced; never "completes" (the adjudication loop is the steady state of a living
  styleguide).

## Sub-track DAG

```
E0 seed ✓ ──► V1a mining (3 ∥-capable sessions) ──► J-E1 ──► V1b authoring (3 interactive sessions) ──► v1 ✓ ──► J2 (ROADMAP.md)
                                                                                                          │
                                                                     ┌────────────────────────────────────┼──────────────────┐
                                                                     ▼                                    ▼                  ▼
                                                              A applications                       P public spec        C CEv3 plugin
                                                              (code shards)                        (= R6e, ROADMAP.md)  (own ROADMAP when real)
                                                                     ▲
                                                              L adjudication loop (perpetual; feeds A/P/C)
```

Critical path to J2: **V1a → J-E1 → V1b**.  Post-v1 nodes are unordered among themselves and operator-paced.

### E0 — Seed  (DONE 2026-07-22)

`docs/STYLEGUIDE.md` created: purpose/standing, three foundational principles, five-layer × two-partition architecture,
layer 5 (epistemic register) fully authored, case register seeded with 14 open cases (SEL-1..11, NORM-1..2, REND-1).
Charter + adjudication record in `docs/NOTES.md`.  Rendered-not-buried canonized (rule 5.3); contested-case marking =
sidecar + case-IDs (rule 5.5); flat single document, public spec derived later.

### V1a — Source mining  (Category B; 3 sessions; Sonnet-autonomous with rubric)  (IN PROGRESS 2026-07-23 — sharded to docs/PLAN.md)

The extraction rubric for all three: classify every finding onto the five-layer schema; map onto existing case-IDs where
they fit; mint new cases (append-only, per C-CASE) where they do not; record evidence with enough provenance that V1b can
adjudicate without re-mining.  Census artifacts are evidence reservoirs consumed by V1b — STYLEGUIDE.md never cites them.

1. **V1a-S1 — Mine CE documentation** → `docs/census-ce.md`.  Sources: the Classical Extras plugin documentation and
   README (picard-plugins 2.0 tree) and its user guide; supplementary where CE cites them: MusicBrainz classical style
   guidelines, Picard community classical naming scripts.  Deliverable: the editorial-fork inventory — every CE option and
   default as a documented fork, classified by layer; CE's tag vocabulary and semantics (the compatibility floor,
   enumerated); ordering/grammar conventions.
2. **V1a-S2 — Mine the implementation** → `docs/census-impl.md`.  Deliverable: the de-facto rulings inventory — every
   editorial choice enacted in code (role classification heuristics, credit orderings, separators, composite-tag sources,
   path grammar components, the concerto path-injection, the frozen C-CLASS/C-INIT shapes), each classified
   deliberate-vs-accidental with a ratify/overturn queue for V1b.
3. **V1a-S3 — Mine the library** → `docs/census-library.md` + a read-only scanner in `scripts/` (per the existing
   library-scan script precedent).  Deliverable: the empirical census — per-case frequency estimates (multi-soloist
   releases, conductor-less ensembles, choir+orchestra, completer/arranger credits, play-direct, opera principal counts),
   attribution-variance instances (same work, different credit sets across releases), name-form variance (same artist
   MBID, different rendered forms).  Host-path caveat: run against the canonical library root (or matching mount); a
   mismatched `dest_root` is a silent no-op hazard.

Sessions are mutually independent (parallelizable); S3 alone touches `scripts/` (no `src/` changes, no gate impact).

### J-E1 — mining→authoring juncture

Adjudicates: census quality sufficient for adjudication?  Volume and shape of newly minted cases (a large mint is a
signal the seed taxonomy mis-shaped the space — surface, do not silently absorb); whether V1b's session split still fits
the evidence; any discovered conflict with frozen contracts (C-CLASS/C-INIT) → flag to the library-completion ROADMAP,
never re-open in-arc.

### V1b — Authoring  (3 sessions; interactive — the operator is the editorial authority; architect-on-Fable register)

4. **V1b-S4 — Ontology through the sharp cases** (Category A substrate).  Adjudicate SEL-1 (ambiguous soloist), SEL-2
   (concerto grosso), SEL-6 (play-direct), SEL-11 (canonical-soloist promotion) against the three censuses; author layer 1
   (role taxonomy + the canonical-identity definition) *from* the adjudications.  Freezes **C-ONT**.  The operator's
   pinned focus (NOTES session-1 close) lands here.
5. **V1b-S5 — Remaining adjudications + layers 2–3**.  Rule on the rest of the register (including V1a-minted cases);
   genuinely undecidable cases get a documented neutral default or a documented-open status (both are rulings).
   Generalise the selection (layer 2) and normalisation (layer 3) rules from the accumulated rulings.
6. **V1b-S6 — Rendering + integration → v1** (Category I integrative; consistently under-scheduled — full session
   minimum).  Author layer 4: per-surface grammars, including the `ARTIST`/`ALBUMARTIST` grammars and the path-component
   grammar; write the CE-divergence register (from the S2 ratify/overturn queue); end-to-end coherence pass.  **v1 ✓ —
   report to J2.**

### Post-v1 nodes (trigger/operator-paced; unordered)

- **A — Applications (code shards).**  Each is a normal PLAN-sharded code sub-track once v1 rules exist: the
  `ProvenanceSidecar` editorial-notes field (case-ID persistence, rule 5.5); replacing the mechanical concerto
  path-injection with the SEL-11 ruling; composite-tag grammar changes (`ARTIST` et al.); normalisation changes (canonical
  name-forms in paths).  Application shards that change persisted tags or paths coordinate with the library-completion
  arc's R6 re-derivation — prefer landing them so R6d re-paths once, not piecemeal.
- **P — Public conventions spec.**  The externalised projection of the styleguide (= R6e in the library-completion
  ROADMAP; finalises alongside the Act II freeze).  Derivation, not duplication.
- **C — CEv3.**  The CE successor on Picard v3, platforming the styleguide's MB-derivable partition.  Graduates to its
  own ROADMAP when actioned; first step there is contacting the CE author (response to our extensions and to Picard
  APIv3 — the standing intent to take on/over CEv3).
- **L — The adjudication loop.**  New cases append to the register as annotation work surfaces them; statuses revise as
  evidence improves.  The styleguide's steady state; never a node that completes.

## Cross-arc coupling

- **Gates J2** (`docs/ROADMAP.md` junctures table): V1b completion is the E gate J2 waits on.
- **Consumes frozen contracts** from the library-completion arc: C-CLASS / C-INIT (validate against the styleguide; an
  apparent need to change either is a finding for that arc's boundary, not an in-arc re-freeze), "path is a handle, not a
  manifest", uniform-ceiling/ragged-floor (both already inputs to layer 4).
- **R4b (fragmentation inventory)** runs parallel in the library-completion arc; its findings about attribution-driven
  fragmentation feed the case register (normalisation cases especially).
- **Applications (A) vs R6**: A-shards that re-shape persisted tags/paths should land before or into R6d's one-pass
  re-derivation, so the library is made "more like itself" exactly once under v1 rules.

## Cross-session contracts

**Consumed (frozen upstream):** the STYLEGUIDE architecture (five layers, two partitions, case-ID scheme — E0); epistemic
rules 5.1–5.5 (E0); C-CLASS / C-INIT (library-completion arc); the CE-continuity posture (STYLEGUIDE standing rules).

**Produced:**
- **C-CASE** *(frozen at V1a start)* — case-ID stability: IDs are `<LAYER>-<n>` (`ONT-`, `SEL-`, `NORM-`, `REND-`,
  `EPIST-`), append-only, never renumbered or reused; minting happens in census artifacts and is absorbed into the
  register at V1b.  Load-bearing because sidecars will persist these IDs (rule 5.5).  Prose-enforced.
- **C-ONT** *(frozen at V1b-S4)* — the layer-1 role taxonomy + canonical-identity definition in STYLEGUIDE.md.  Every
  layer-2/3/4 rule and every A-shard consumes it.  Prose-enforced.
- **v1** *(V1b-S6)* — the J2 gate input; the A/P/C substrate.

## Scope estimate (static frame)

E0 1 ✓ · V1a 3 · V1b 3 → **~7 sessions to v1** (6 remaining).  Post-v1: A ~2-4 shards of 1-3 sessions each
(sharded per normal PLAN convention when elected); P = R6e's existing estimate; C unscoped until graduated.

## Out of scope (v1)

- **Code changes** beyond the V1a-S3 scanner script — no `src/` edits; every application is a post-v1 A-shard.
- **Re-opening C-CLASS/C-INIT** — validate only; conflicts are findings for the library-completion arc's boundary.
- **Completeness** — v1 freezes architecture + method; rulings are revisable by design (the L loop).
- **CEv3 implementation and CE-author contact** — node C, post-v1.

## Discoveries appendix

(Mid-session discoveries append here; evaluated at the next sub-track boundary.)
