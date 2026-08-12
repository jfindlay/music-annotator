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

### V1a — Source mining  (Category B; 3 sessions; Sonnet-autonomous with rubric)  (DONE 2026-07-23 — commits dcf55f2 / 45681bd / 58f7f9b; J-E1 verdict: still-on-intent)

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

**Fired 2026-07-22 at the S3 ◆ (boundary-transform fork): still-on-intent.**  62 cases minted across V1a (35 S1 · 24 S2
· 3 S3) against the 14-case E0 seed — adjudicated a volume signal (evidence density), not a taxonomy mis-shaping; the
five-layer schema held with no schema-fit failure.  V1b's 3-session split confirmed still fitting the evidence.  Two
operator recommendations carried to V1b (see Discoveries appendix D-A1/D-A2).

### V1b — Authoring  (3 sessions; interactive — the operator is the editorial authority; architect-on-Fable register)  (DONE 2026-07-30 — v1 ✓, reported to J2; S4 3a1d58f · S5 38b1559 · S6 see PLAN ledger)

4. **V1b-S4 — Ontology through the sharp cases** (Category A substrate).  Adjudicate SEL-1 (ambiguous soloist), SEL-2
   (concerto grosso), SEL-6 (play-direct) against the three censuses; record SEL-11 (canonical-soloist promotion) as
   **overturned** — pre-adjudicated by the operator (2026-07-23): no path promotion, the concerto path-injection is
   dropped (REND-16 moot with it — see Discoveries D-A3).  Author layer 1 (role taxonomy + the canonical-identity
   definition) *from* the adjudications.  Freezes **C-ONT**.  The operator's pinned focus (NOTES session-1 close) lands
   here.
5. **V1b-S5 — Remaining adjudications + layers 2–3**.  Rule on the rest of the register (including V1a-minted cases);
   genuinely undecidable cases get a documented neutral default or a documented-open status (both are rulings).
   Generalise the selection (layer 2) and normalisation (layer 3) rules from the accumulated rulings.
6. **V1b-S6 — Rendering + integration → v1** (Category I integrative; consistently under-scheduled — full session
   minimum).  Author layer 4: per-surface grammars, including the `ARTIST`/`ALBUMARTIST` grammars and the path-component
   grammar; write the CE-divergence register (from the S2 ratify/overturn queue); end-to-end coherence pass.  **v1 ✓ —
   report to J2.**

### Post-v1 nodes (trigger/operator-paced; unordered)

- **A — Applications (code shards).**  Each is a normal PLAN-sharded code sub-track once v1 rules exist.
  **DONE (2026-07-31, sub-track "A-shards", `docs/PLAN.md` 4/4):** removing the concerto path-injection
  (SEL-11 overturned) and the S6 tag-shaping set — REND-14 billing-order reorder + composite-tag naming
  realignment, chorusmaster-into-`CONDUCTOR`, `IS_CLASSICAL` conditionalisation — all landed ahead of R6d
  (froze C-NOSOLO + C-RA-GRAMMAR; ◆ still-on-intent).  **Remaining node-A shards (not yet sharded):** the
  `ProvenanceSidecar` editorial-notes field (case-ID persistence, rule 5.5); composite-tag grammar changes
  (`ARTIST` et al.); normalisation changes (canonical name-forms in paths).  Application shards that change
  persisted tags or paths coordinate with the library-completion arc's R6 re-derivation — prefer landing
  them so R6d re-paths once, not piecemeal.
  **DONE (2026-08-11, sub-track "sidecar-case-ids", `docs/PLAN.md` 3/3; commits `856dfec` / `55dd0c6` /
  `a3a9e0c`+`441bddb`; ◆ still-on-intent):** the rule-5.5 `ProvenanceSidecar` case-ID persistence shard —
  the `applied_case_ids` field + set-union monotonic merge, sourcing + threading of the run-derived
  contested-default (P2) case-IDs, and the `audit`-surface enumeration all landed.  **Froze C-CASE-PROV**
  (field + set-union-append-only merge; source set {SEL-11 run-derived; REND-1/REND-2/REND-14 structural;
  NORM-1/NORM-2 no clean application site}).  Sidecar-only — no persisted-tag or path change, no R6d coupling.
  **DONE (2026-08-12, sub-track "path-canonical-name-forms", `docs/PLAN.md` 3/3; commits `e0f7c3a` /
  `575169d` / `238fde0`; ◆ still-on-intent):** the normalisation shard — render STYLEGUIDE 3.1/NORM-2
  canonical entity name-forms in the compact path projection (4.5), sourced from MB's own primary-flagged
  aliases (authority-deference posture, D-A7/D-A8).  **Froze C-CANON** (`MBArtist.alias_list` +
  `canonical_artist_form` resolver; alias source = a dedicated `fetch_artist_aliases(mbid)` on the two-layer
  defensive-download path with an `_ARTIST_CACHE`, *not* the release-fetch `"aliases"` include — the
  webservice does not reliably emit sub-entity aliases on a release query; plus the `MBAlias` raw-key remap
  `"alias"`→`name`).  **Path-changing but code-only now**: new ingests render canonical; the destructive
  library-wide repath rides R6d's one pass (D-A5 precedent), accepted temporary as-credited/canonical
  library inconsistency until then (D-A4-style).  Substrate survey findings folded: composite-tag grammar
  (`ARTIST` et al.) is **discharged by v1's enacted state** (ARTIST/ALBUMARTIST already verbatim by design —
  REND-1/4.3; not a defect), and the D-A6 "3.1-vs-REND-1 conflict" is **dissolved** (they govern different
  surfaces — see D-A6/D-A7).  **With this shard, node A has no auto-obvious remaining agent-shardable target:**
  the three originally-enumerated shards (editorial-notes field, composite-tag grammar, normalisation) are all
  done or discharged; further A-shards are L-loop/operator-elected.
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

E0 1 ✓ · V1a 3 ✓ · V1b 3 ✓ → **v1 reached in 7 sessions (2026-07-30)**.  Post-v1: A ~2-4 shards of 1-3 sessions each
(sharded per normal PLAN convention when elected); P = R6e's existing estimate; C unscoped until graduated.

## Out of scope (v1)

- **Code changes** beyond the V1a-S3 scanner script — no `src/` edits; every application is a post-v1 A-shard.
- **Re-opening C-CLASS/C-INIT** — validate only; conflicts are findings for the library-completion arc's boundary.
- **Completeness** — v1 freezes architecture + method; rulings are revisable by design (the L loop).
- **CEv3 implementation and CE-author contact** — node C, post-v1.

## Discoveries appendix

(Mid-session discoveries append here; evaluated at the next sub-track boundary.)

- **D-A1 (2026-07-22, V1a ◆ / J-E1).**  62-case mint across V1a; J-E1 verdict still-on-intent — the volume is evidence
  density, not schema failure.  Operator recommendation carried to V1b-S6: **REND merge-assessment** — 26 REND cases;
  REND-14/15/16 (ordering family) and REND-17/18 (separator family) are consolidation candidates.  Merges are
  cross-referencing adjudications, never renumbers (C-CASE).
- **D-A2 (2026-07-22 S3; resolved 2026-07-23).**  `census-library.md` was produced from documentary evidence
  (census-r0, NOTES, BACKLOG) because the canonical library root was not mounted in the dev environment.  The operator
  reviewed the census and **cleared V1b to proceed on this basis (2026-07-23)** — the hades scanner re-run is waived as
  a V1b precondition and remains available to the post-v1 L loop.  V1b rules with existence-weight, not
  frequency-weight, on library evidence.
- **D-A3 (2026-07-23, operator).**  **The concerto:soloist hack is dropped.**  SEL-11 (canonical-soloist promotion) is
  pre-adjudicated **overturned** — a concerto release always carries the soloist in its tags; nothing is promoted into
  the path grammar.  REND-16 is moot with it; the `_tags.py:1189` gate removal is a trivial post-v1 A-shard
  (coordinated with R6d).  R4c's dissolved need is thereby resolved by *rejection*, not generalisation.
- **D-A4 (2026-07-30, S6 ◆).**  **v1 ✓.**  All 26 REND IDs statused (merges: REND-16→SEL-11, REND-18→REND-6,
  REND-8/9/11→REND-5); the D-S2-1 tag-vs-path "inversion" dissolved (the path already renders billing order; REND-14's
  tag assembly was the deviant surface, overturned in part by the operator's normalise-to-billing-order ruling).  New
  divergences: REND-1 (no composer fallback into `ARTIST`), REND-2 (no composer prefix on `ALBUM`), REND-14 (billing
  order over CE assembly order); CE convention adopted for the chorusmaster in `CONDUCTOR`.  A standing-rule-2
  naming-drift hazard (composite semantics under CE's verbatim-credit tag name) is queued for realignment with the
  REND-14 shard.  The A-node gains the S6 tag-shaping set; all coordinate with R6d.

- **D-A5 (2026-07-31, A-shards ◆).**  The S6 tag-shaping set is enacted (`docs/PLAN.md` 4/4).  The
  **standing-rule-2 naming-drift hazard queued at D-A4 is resolved**: the S2 inflection juncture found the
  queued premise ("composite semantics under CE's verbatim-credit tag name") imprecise — CE's
  `_cea_recording_artist` denotes the *assembled* composite (census-ce.md), and the verbatim credit already
  lives under `CEA_MB_ARTISTS`/`ARTIST`.  **Ruling: keep the composite under `CEA_RECORDING_ARTIST`, no
  rename, no new verbatim tag**; the only CE divergence is the billing-order assembly (REND-14), an
  already-registered divergence.  No library-wide tag rename at R6d.  C-NOSOLO + C-RA-GRAMMAR froze.

- **D-A6 (2026-08-09, sidecar-case-ids shard).**  Substrate survey for the three remaining node-A shards
  found the **composite-tag-grammar shard largely discharged by v1**: `ARTIST`/`ALBUMARTIST` already render
  the verbatim MB credit with no author-splicing (REND-1/4.3 satisfied), and `CEA_RECORDING_ARTIST`
  (assembled) / `CEA_MB_ARTISTS` (verbatim) are already correctly separated (C-RA-GRAMMAR, A-shards S2).  No
  un-enacted grammar work was found without an operator-named target; the shard is provisionally discharged,
  not sharded.  Separately, the **normalisation shard (canonical name-forms) carries an unresolved design
  conflict**: STYLEGUIDE 3.1 (compact projections render canonical forms) vs. REND-1/4.3 (`ARTIST` is a
  *preserved verbatim* claim) — making `ARTIST` canonical would contradict REND-1.  The conflict needs
  adjudication (which surfaces render canonical vs. preserved forms) before the normalisation shard is
  shardable; and it triggers a library-wide repath that must coordinate with R6d.  Neither is a blocker for
  the sidecar-case-ids shard (sidecar-only, R6d-independent).
  **SUPERSEDED (2026-08-11, D-A7): the conflict was a category error.**  3.1/NORM-2 govern the *compact*
  projection (the path, 4.5); REND-1/4.3 govern `ARTIST`/`ALBUMARTIST` (*preserved/full* surfaces, 4.3).  The
  v1 register-split (4.1 assembled-vs-preserved; 3.2 compact-vs-full) already resolves them to different
  surfaces — there is nothing to adjudicate between the two rules.  `ARTIST` stays verbatim; the path renders
  canonical.  The normalisation shard *is* shardable (see D-A7).

- **D-A7 (2026-08-11, path-canonical-name-forms shard boundary — supersedes the D-A6 conflict).**  Two
  operator rulings unblock the normalisation shard.  (1) **No rule conflict** — 3.1 (path, compact) and
  REND-1/4.3 (`ARTIST`, preserved) apply to different surfaces (D-A6 superseded above).  A code audit found
  the real gap: **no canonical-form machinery exists anywhere in `src/`** — all path name-forms render
  verbatim from `MBArtist.name` / `sort_name` (`_tags.py:430`, `:587`, `:1224`), so the path can render
  "Vienna Philharmonic" where NORM-2 demands "Wiener Philharmoniker".  The shard has genuine work.  (2)
  **Canonical-form source = MB's own primary-flagged aliases** (operator, authority-deference posture): fetch
  the artist alias-list, prefer the primary-flagged alias per NORM-2's native/reception rule, fall back to
  `MBArtist.name`.  The *only* editorial act is selecting among MB's own asserted forms — never a local
  editorial table, never a form MB does not hold.  (3) **Sequencing: code-only now, repath rides R6d** (D-A5
  precedent) — new ingests render canonical; the destructive library-wide repath defers to R6d's one pass
  under J3.  Temporary as-credited/canonical inconsistency in the library until R6d, accepted.

- **D-A8 (posture, 2026-08-11) — MB-authority deference (durable design posture).**  Operator: accept MB data
  as the source of authority even where fallible/incomplete; modify it only "as defensibly and plainly as
  possible"; **introduce no new conventions in annotation style or music scholarship**.  Improving on MB to
  serve one collector's taste does not scale across users or time; the good-engineering choice is to defer to
  MB's own assertions and select among them plainly.  This sharpens the arc's *generative-neutrality* and
  *CE-continuity* postures into an authority-deference rule that anchors this shard (canonical form = MB's own
  primary alias, not an authored form) and every future application shard.  Prose-enforced; belongs in the
  styleguide's foundational-principles register at the next V1b/L-loop touch.

- **D-A9 (2026-08-12, path-canonical-name-forms ◆ — durable substrate fact for the L-loop / any future
  alias-consuming shard).**  The canonical-form resolver (C-CANON) sources aliases via a **dedicated
  `fetch_artist_aliases(mbid)`** (artist as the *direct* query target), **not** the `"aliases"` include on a
  release/recording fetch: the MB webservice does not reliably emit `<alias-list>` for artists nested in
  `artist-credit`/relations on a release query (the documented library-vs-REST gap; AGENTS.md), so the
  include path is an unsound foundation for a resolver that must see the complete `primary`/`locale`-flagged
  alias set to select "once, not per release".  Two enacted subtleties any future alias work inherits: (i)
  the raw musicbrainzngs alias key is **`"alias"`**, not `"name"` (`MBAlias` remaps it — previously latent);
  (ii) credit/relation artists off a release fetch carry `alias_list == []`, so a consumer must **hydrate via
  `fetch_artist_aliases`** before calling `canonical_artist_form` or silently get the `name` fallback.
  Load-bearing but not live-verified (no network in the dev environment); the dedicated-fetch ruling is
  robust either way (partial propagation would still be incomplete).  Prose-enforced; no schema re-open.
