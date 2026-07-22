# The Editorial Styleguide for Annotating Recorded Art Music

This document is the editorial basis of music-annotator: the articulated set of principles from which every attribution,
annotation, naming, and rendering decision derives.  It is a *generative* styleguide — authored from principle so that it
consults well both for the cases enumerated here and for cases not yet discovered — rather than an accumulated list of
per-case rules.  It is a living document: cases are adjudicated into it over time, and every adjudication cites the
principles it derives from.

## Purpose and standing

**Universality.**  The styleguide answers "how should recorded art music be tagged and organised?" independently of any
implementation.  It is realised by music-annotator directly (a standalone annotator with full filesystem and provenance
machinery) and is intended as the philosophical basis of a successor to the Classical Extras Picard plugin ("CEv3") on the
Picard v3 API.  music-annotator will eventually serve both roles — tagging within and without Picard — from this one basis.

**Classical Extras continuity.**  Classical Extras (CE) is the anchor convention: it encodes a coherent, field-tested
stance on how art-music recordings should be described, and this styleguide builds on that contract rather than
reconstructing it.  Three standing rules govern the relationship:

1. CE (and, where live conventions exist, Picard) tag semantics are the compatibility floor.  Shared tag names keep their
   established meanings.
2. Extensions are additive: new semantics always get new tag names, never a redefinition of a CE or Picard tag.
   Fragmentation is *same name, different semantics*; additive extensions do not fragment.
3. Divergences from CE are permitted only with a documented rationale, recorded in this document's case register.

**Relationship to the public conventions spec.**  A public specification of the implemented tag set and rendering rules is
derived *from* this styleguide once conventions freeze; the styleguide is the internal generative basis, the spec is its
externalised projection.  The two are distinct documents so that generative authoring here is not constrained by
public-register prose obligations.

## Foundational principles

**P1 — Cross-surface coherence.**  Directory paths (compact), tags (full), and playlists (full) are renderings of *one*
attribution model — never independent rules that happen to agree.  Compact projections are UX-ceiling-bounded and carry only
the audible principals; full projections carry complete credits.  A rendering may *omit* relative to the model; it may never
*disagree* with it.  Composite tags are projections in exactly this sense: a tag such as `ARTIST` is a defined grammar over
the attribution model (ordered role classes, separators, inclusion policy), structurally identical to a path component and
differing only in ceiling and completeness.

**P2 — Generative neutrality.**  Publishers and editors legitimately differ; the same work is attributed differently across
releases, exactly as publishing houses' style guides differ.  Where sources disagree, this styleguide takes as neutral a
defensible position as it can; where it must choose, the choice and its rationale are documented as a registered case.  The
styleguide must consult well for undecided cases: a rule that only restates its examples has failed this principle.

**P3 — Annotation as claim.**  An annotation is a claim, not a fact; the library records claim *and basis*.  Annotation is
scholarship under irreducibly incomplete and contradictory sources — the histories and artifacts of composition and
recording are incomplete, and error is inevitable.  The styleguide therefore treats confidence, provenance, and known
contestation as first-class content (the epistemic register, layer 5), within a strict boundary: tags and their companions
carry *curation-grade* epistemics (what is claimed, on what basis, at what confidence); *scholarship-grade* argument —
evidence, disputation, correction of the world's sources — routes upstream to the shared databases and the scholarly
record, never into tags.  Biblioteconomy in the library; scholarship in the commons.

## Architecture: five layers, one partition

Every rule in this styleguide belongs to one of five layers, each consuming the one above it:

| # | Layer | Question it answers |
|---|-------|---------------------|
| 1 | **Ontology** | What entities and roles exist?  What is a work's canonical identity? |
| 2 | **Selection** | Who is attributed, at each scope (work / recording / release)? |
| 3 | **Normalisation** | Which name-form renders an identity? |
| 4 | **Rendering** | How does each surface (path, each composite tag, playlist) render the model? |
| 5 | **Epistemic register** | With what confidence, provenance, and contestation marking? |

Orthogonally, every rule lands in one of two **platform partitions**:

- **MB-derivable** — computable deterministically from MusicBrainz data plus explicit configuration: attribution selection,
  normalisation, composite-tag grammars, work-title policy.  This partition is implementable by a tag-only platform such as
  a Picard plugin and constitutes the CEv3-implementable surface.
- **Library-level** — requiring filesystem, provenance, or operator machinery: path construction, transaction journaling,
  confidence persistence, sidecars, playlists, physical-media attestation.  Available to music-annotator standalone; not to
  a tag-only platform.

Cross-surface coherence (P1) governs both partitions from the one model; the partition determines only where a rule can
execute.  A rule in the MB-derivable partition must never quietly depend on library-level machinery.

## Layer 1 — Ontology (to be authored)

The role taxonomy.  Its working spine: recordings are traditionally attributed to three performer categories, in the order
**soloists → conductors → ensembles**, mirroring how visible and audible credits are arranged — the film-credits stance:
attribute the principals the audience perceives, not every contributor.  The layer must also define the composer-side roles
(composer, arranger, orchestrator, completer, cadenza author, transcriber), the auxiliary roles (chorusmaster, continuo,
leader/director), and the concept of a work's *canonical identity* — the properties (including performer roles such as a
concerto's soloist) that belong to what the work *is*, rather than to any particular performance of it.

## Layer 2 — Selection (to be authored)

Who is attributed, per scope.  All three spine categories are normally attributed; ambiguity lives in the edge cases, which
are adjudicated through the case register (`SEL-*`).  The layer owes: the principals-versus-support distinction, the rule
for promoting a performer into a work's canonical identity (the concerto-soloist question generalised), and the treatment
of composer-side selection (when a completer or orchestrator is attributed alongside the composer).

## Layer 3 — Normalisation (to be authored)

Which name-form renders an identity.  The seed rule, motivated by the fragmentation problem (one performer's credit
rendered differently across releases fragments any surface keyed on the rendered form):

- **Compact projections (paths) render canonical, identity-stable name-forms** — the entity's canonical name, not the
  per-release credit — so that one entity occupies one place.
- **Full projections (tags) render the canonical form *and* carry as-credited variants** where they differ, so no credit
  information is lost.

The layer owes: the native-language/native-script policy, the treatment of entities whose names legitimately change over
time (`NORM-*` cases), and the interaction of normalisation with sort-name derivation.

## Layer 4 — Rendering (to be authored)

Per-surface grammars over the model: path components, each composite tag (`ARTIST`, `ALBUMARTIST`, and the extended
classical tag families), playlists.  Each grammar declares its ordering (the spine order unless a case rules otherwise),
its separators, its inclusion policy, and its ceiling.  Two rendering rules are already converged and are inputs to this
layer rather than open questions:

- **Path is a handle, not a manifest.**  The destination directory and filename are short, stable identifiers a user
  locates a recording by — not manifests of every credited contributor.  Full credits belong in full projections.
- **Uniform ceiling, ragged floor.**  When projecting a work's part-hierarchy onto directory depth, over-resolved branches
  clamp down to the group's modal depth (removing structure the path does not need — faithful), but shallow branches are
  never padded up (which would invent structure that is not there — unfaithful).

## Layer 5 — The epistemic register

The realisation of P3.  Four rules and a marking mechanism.

**5.1 Claim and basis.**  Every annotation the system renders is a claim backed by a recorded basis.  Two orthogonal
confidence ladders realise this at coarse grain: an *identity-confidence* ladder (how confidently a file matches the
recording it is claimed to be) and an *annotation-completeness* ladder (how completely a release could be annotated from
available sources).  Both are persisted with the annotated material, so degradation is always explicit: an entry annotated
from partial sources *says so*, permanently, in the unit itself.

**5.2 Never silently degrade.**  Deliberate degradation (ingesting at partial confidence, substituting a fallback basis) is
permitted only when persisted as a first-class fact.  The discrimination underneath is *failure versus no-data*: "the
source answered that no data exists" is legitimate emptiness; "the data could not be determined" is an error, and an error
is never rendered as if it were emptiness.

**5.3 Rendered, not buried.**  Where a fallback basis is used and the surface can afford it, the rendered form itself
carries the basis.  The exemplar: a recording-session year renders as `[rec 1984]`, but when only a release year is
available the label *changes form* to `[rel 2000]` — the reader sees the basis of the claim in the claim itself, at zero
side-channel cost, surviving every copy and export.  Rendering decisions in layer 4 must consider a visible-basis form
before reaching for a side channel.  The principle yields to ceilings: where a visible mark would deface a compact surface
(an asterisk in a path; an annotation inside `ARTIST`), the mark moves to the mechanism of 5.5.

**5.4 Identity honesty.**  Claims flow only along verified identity edges.  Where identity is knowingly approximated — for
example, annotating from a parallel release of the same recordings when the exact pressing is absent from the source
database — the approximation is a persisted fact, never a silent substitution.  Upstream the same principle is a submission
bar: data is contributed to the shared databases only when attested against the correct entity, with physical media as the
ground truth where doubt exists.  Structural disagreements between local material and database records (track-count
mismatches, layout differences) are physical-world facts: the annotator surfaces and defers to the operator; it never
guesses.

**5.5 Contested-case marking.**  Where releases or editors legitimately disagree and this styleguide has chosen a neutral
default (P2), applying that default is itself an annotation-on-the-annotation.  The mechanism: every adjudicated case in
the register below carries a stable case-ID; implementations that maintain per-release provenance sidecars record the
applied case-IDs there — claim in the unit, prose in this document, nothing free-text in tags.  Tag-only platforms (the
MB-derivable partition) apply the same defaults without persisting the mark: an honest capability difference, not a
coherence break, because the default itself is deterministic from this document either way.

## Case register

Each case is a fact pattern that has been observed (or is confidently expected) to be attributed or rendered differently
across releases or editors — proof that the answer is editorial, not mechanical.  Cases carry a stable ID
(`<LAYER>-<n>`: `ONT-`, `SEL-`, `NORM-`, `REND-`, `EPIST-`), a status (**open** / **adjudicated** / **divergence** — the
last meaning a documented departure from CE or Picard convention), and, once adjudicated, the ruling with the principles it
derives from.  The register is seed, not closure: new cases append.

### Selection

- **SEL-1 (open) — Ambiguous soloist role.**  Albinoni's Adagio in G minor: attribute the organ soloist, the violin
  soloist, both, or neither?
- **SEL-2 (open) — Concerto grosso.**  Multiple concertino soloists and no single "the soloist" — the historically common
  case; the modern single-soloist concerto is the special case.
- **SEL-3 (open) — Independent choral ensemble.**  Orchestra joined by a guest choir: is the chorusmaster attributed
  alongside the conductor?
- **SEL-4 (open) — Ensemble works with unique parts.**  Modern works written for named soloists, or where each performer
  plays a unique part, yet attribution conventionally goes to the ensemble.
- **SEL-5 (open) — Guest soloists within an ensemble.**  An ensemble joined by guests covering some (not all) solo parts:
  mixed individual-plus-ensemble attribution.
- **SEL-6 (open) — Play-direct.**  A soloist directing from the instrument: soloist, conductor, or both categories at
  once?
- **SEL-7 (open) — Opera principals.**  Are named-role singers "soloists"?  A principal cast of six meets the compact
  ceiling head-on.
- **SEL-8 (open) — Completers and orchestrators.**  Süssmayr's Requiem completion, Cooke's Mahler 10, Ravel's *Pictures*:
  composer credit, arranger credit, or both — and does the completer enter the compact projection?
- **SEL-9 (open) — Transcription chains.**  Bach–Busoni and kin: how does a transcription attribute its chain of authors?
- **SEL-10 (open) — Anonymous and traditional works.**  Selection when there is no composer to select.
- **SEL-11 (open) — Canonical-soloist promotion.**  When is a soloist part of the work's canonical identity (and thus
  promoted into compact projections) beyond the mechanical concerto case — organ symphonies, works written *for* a
  soloist, symphony-with-obbligato?

### Normalisation

- **NORM-1 (open) — Historical ensemble renames.**  One entity, era-dependent names: which form renders, and does the
  performance date select it?
- **NORM-2 (open) — Native language and script.**  Rendering names and titles for entities whose native form is not
  Latin-script, and titles whose authentic form differs from their reception-history form.

### Rendering

- **REND-1 (open) — Composer in `ARTIST`.**  For classical recordings, does the `ARTIST` grammar lead with the composer
  (as several established house styles do) or carry performers only?  Releases genuinely disagree; P2 applies.
