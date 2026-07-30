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

## Layer 1 — Ontology

The role taxonomy and the canonical-identity definition.  Authored from the adjudication of the sharpest selection cases
(SEL-1, SEL-2, SEL-6, SEL-11) rather than in the abstract; the rulings live in the case register, the generalised taxonomy
here.

**1.1 The performer spine.**  Recordings are attributed to three principal performer categories, in the order **soloists →
conductors → ensembles**, mirroring how visible and audible credits are traditionally arranged — the film-credits stance:
attribute the principals the audience perceives, not every contributor.  The spine defines *positions*, not a partition: a
performer may occupy more than one position at once (play-direct, SEL-6), and any position may be empty or multiply occupied
(a concerto grosso has no default soloist, SEL-2; a triple concerto has three).

**1.2 The soloist position.**  A performer is a soloist **iff reasonable confidence establishes the part as a named or
attributive solo**.  Confidence draws on two independent, covariant streams: the *descriptive* stream (the crediting history
of the work and release — the source database's conventions and editorial work, the recording label's, the publisher's and
engraver's before that, and every incidental force in the work's transmission) and the *normative* stream (the defensible,
wise optimum this styleguide strives for).  The two usually align; where they diverge, this styleguide's judgment governs
the rendered projections, and the release's own crediting survives as evidence (P3: an annotation is a claim, and the credit
is part of its basis).

Sources of reasonable confidence:

- **Work format.**  Formats that constitutively name the solo role attribute the soloist by construction: concerto, organ
  symphony, lied and song cycle, sonata-with-accompanist, and kin.
- **Traditional attribution.**  Where performance practice and reception history name the part, the position is supported
  even without a constitutive format.

Negative rules — each rebuts an expansive reading of the position:

- **Prominence is not solohood.**  Chamber players are not soloists however prominent their material, and independent
  musicians collaborating on a chamber recording remain chamber players.  A contemporary work titled "for three soloists"
  does not mechanically confer the position — the title may itself be a theoretical exploration of the term.
- **Orchestral principals are not soloists.**  Principal string, wind, and brass players routinely render solo passages —
  the concertmaster in Scheherazade holds forth extensively — and are traditionally unnamed: the part belongs to the chair,
  not to a named guest, and it would be unusual for an orchestra to engage an external player for it.  Percussion is the
  reductio: under an expansive definition every percussionist would be a soloist.
- **Era sensitivity.**  Baroque and earlier repertoire predates the ossification of the solo–ensemble spectrum; concertino
  parts and obbligato lines (the concerto grosso; the Albinoni Adagio's organ and violin) do not confer soloist positions by
  default.  Exceptional engagement can establish them (four celebrated violinists engaged for Vivaldi's Concerto for four
  violins).
- **Ensemble-name precedence.**  Performers collectively known under an ensemble name are attributed as the ensemble, never
  as individuals.

Demotion from the soloist position never deletes a credit: an individually-credited non-soloist remains a performer in the
full projections, with instrument and as-credited form intact (P1 — a rendering may omit, never disagree).  The soloist
position is an editorial category over the credits, not a container for them.

Within the position, sub-classification serves tag routing, not spine order: *vocalists* (voice-type labels),
*instrumentalists* (instrument labels), *other soloists* (no label).  The label vocabulary is a known-imperfect heuristic —
"bass" names both a voice and an instrument (ONT-9) — and explicit voice-type evidence is preferred where present.

**1.3 Conductors and dual occupancy.**  The conductor position is occupied by performers credited as conductor.  A
play-direct performer — the soloist directing from the instrument — occupies both the soloist and conductor positions at
once (SEL-6): both roles are real, and full projections carry both.  Compact projections render the performer once, at the
soloist position; the contraction toward the instrument is itself the traditional direction (a play-direct recording is
billed under the performer-at-the-instrument first).

**1.4 Ensembles.**  Orchestras, choirs, and chamber groups.  Classification is by collective identity: a group known under
an ensemble name is an ensemble, and its members are attributed through it (ensemble-name precedence, 1.2).  Name-vocabulary
matching (orchestra / philharmonic / choir / quartet / …) is a serviceable mechanical heuristic for the category, with
documented edge cases (ONT-7).

**1.5 Auxiliary performer roles.**  Positions held in the taxonomy now even where their selection rules wait for layer 2:
**chorusmaster** (attribution alongside the conductor is SEL-3), **concertmaster/leader**, **continuo**, **guest soloists
within an ensemble** (SEL-5), **opera principals** (SEL-7), and **vocal soloists in choral works** (SEL-22).  Adding a
position later is costlier than carrying one.

**1.6 Composer-side roles.**  Authorship positions, attributed at the work scope: **composer**; **additional/assistant
composer** (the usual database realisation of a completer — Süssmayr's Requiem); **arranger**, **orchestrator**,
**reconstructor**, **revisor**; **transcriber** (the Bach–Busoni chain, SEL-9); **cadenza author**; **writer** — a distinct
authorial position at the work scope, though recording-scope practice has merged it into composer (the asymmetry is
adjudicated deliberate at SEL-18); **lyricist**, **librettist**, **translator**.  The positions are fixed here; the selection rules —
when a completer is attributed alongside the composer, whether the completer enters compact projections (SEL-8) — are
layer-2 rulings.

**1.7 Canonical identity of a work.**  A work's canonical identity is its **compositional identity**: the properties fixed
by the act and record of composition — title, key, catalogue and opus designation, work type, compositional structure
(movements, parts, a containing cycle), compositional dates, and authorial lineage (the composer; for arrangements,
completions, and transcriptions, the chain of authors).  **No performer role is part of a work's canonical identity**
(SEL-11, adjudicated): a concerto is *for* a soloist but for no particular soloist; the soloist is a property of a
performance, never of the work.  The consequences are structural: compact projections carry the work's compositional
identity plus the performance's stable identity signals; nothing performance-level is ever promoted into the work's
identity.

**Scope boundary — improvisational primacy (ONT-11).**  The definition above holds for art music in the written tradition,
where the written record — and the scholarship between it and us — is the provenance of the work; the improvisational era of
classical performance is long over, its improvisations remembered, recorded, and studied.  Forms with improvisational
primacy — jazz above all — invert the authority: the audio capture itself is the authoritative record, and the performers
are constitutive of the recorded work's identity.  Such repertoire sits at this styleguide's boundary; the inversion is
registered as ONT-11 (open) rather than forced into the written-tradition model.

## Layer 2 — Selection

Who is attributed, per scope.  Selection operates at three scopes — work, recording, release — and does two distinct jobs:
admitting credits into the attribution model, and assigning performers to the layer-1 positions.  The `SEL-*` rulings in the
case register are the case law of these rules; the rules generalise the rulings, not the reverse.

**2.1 Total selection, editorial positions.**  Every credit the sources carry is selected into the model at its scope —
selection never deletes.  Deletion upstream of rendering would make a surface's omission indistinguishable from absence,
breaking P1's guarantee that a rendering may omit but never disagree.  The editorial act is position assignment: who
occupies the spine positions (1.2–1.4) and which auxiliary and authorial positions are occupied (1.5–1.6).  An
individually-credited performer who occupies no position is a credited performer — fully present in full projections,
absent from compact ones.

**2.2 Principals and support.**  The principals of a recording are the occupants of the spine positions plus the canonical
author chain (1.7).  Everything else — auxiliary position-holders (chorusmaster, leader, continuo), credited non-principal
performers, production credits — is support.  Compact projections render principals only, subject to layer-4 ceilings; full
projections render everything.  The chorusmaster is the boundary exemplar (SEL-3): a real position, attributed alongside
the conductor in full projections, never compact-rendered, never merged into the conductor position.

**2.3 Performer-side selection.**  The 1.2 confidence rule does all the work; the adjudicated cases apply it:

- Named solo parts confer the position by work format: concerto soloists, opera principals (SEL-7), named vocal soloists in
  choral works (SEL-22).
- Collective identity precedes individual prominence: ensemble-name precedence for unique-part works (SEL-4); concertino
  members are not soloists by default (SEL-2, SEL-21).
- Guest status is credit metadata, not a position (SEL-5); the solo/guest/additional attributes are selection evidence only
  (ONT-1).
- The principal–comprimario line in staged works follows reasonable confidence, with the release's own billing as the
  default descriptive evidence (SEL-7).

**2.4 Composer-side selection.**  The authorial chain of the performed edition is canonical (1.7) and is always attributed:
the primary composer leads; completers, orchestrators, and reconstructors of the performing edition are attributed
alongside, role-annotated, in compact as well as full projections (SEL-8); transcription chains are attributed source-first
(SEL-9).  Incidental editorial work — critical editions, continuo realisations — is credited in full projections only.
Anonymity is rendered honestly ("Anonymous", "Traditional"), never filled by promotion; where the performed work is an
arrangement-work, its author chain terminates at the arranger, its terminal author (SEL-10).  The writer/composer
distinction is preserved at work scope and merged at recording scope as CE compatibility floor (SEL-18); composer surfaces
prefer work-scope authorship (SEL-19).

**2.5 Credit routing is not position selection.**  The mechanical role buckets inherited from CE (vocalists,
instrumentalists, other soloists, ensembles, and kin) remain valid as credit containers with their established CE semantics
— the compatibility floor for every CE-named tag (SEL-17).  The layer-1 soloist position (1.2) is strictly narrower than
the soloist buckets.  Any surface that projects a *position* — the path's performer components, any future
position-semantic tag — must consume position selection under 1.2, never bucket contents.

**2.6 Derived-metadata selection.**  Genre derives primarily from work type — compositional identity (1.7) — with reception
sources admissible as secondary evidence and artist inference excluded as basis-free (SEL-14; P3).  Classical
classification is selective and evidence-driven, never blanket (SEL-15).  The composed date is the canonical work date;
published and premiered dates are fallback bases carried with visible basis where the surface affords it (SEL-16; 5.3).

## Layer 3 — Normalisation

Which name-form renders an identity.  The layer's founding problem is fragmentation: any surface keyed on rendered forms
scatters one entity across as many places as it has credit variants.

**3.1 One canonical form per entity.**  Every entity has exactly one canonical name-form, selected once, not per release:
the entity's native name where it is Latin-script (*Wiener Philharmoniker*, not "Vienna Philharmonic"); the established
Latin reception form where the native script is not Latin (*Tchaikovsky*, *Shostakovich*) — the form a reader recognises,
not a scholarly romanisation (NORM-2).  Aliases and credit variants are evidence for choosing this form, never per-release
replacement mechanisms (NORM-3; NORM-4 dissolves).

**3.2 Compact renders canonical; full preserves the credit.**  Compact projections render only canonical forms, so one
entity occupies one place.  Full projections render the canonical form and carry as-credited variants wherever a release's
credit differs — no credit information is lost (P3: the credit is part of the claim's basis).  Entities whose names
legitimately change over time render under the current canonical name in compact projections; the era-correct credit
survives as-credited (NORM-1).  The accepted cost is anachronism in the handle; the rejected cost — one entity fragmented
into many places — would defeat the surface's purpose.

**3.3 Instruments invert the rule.**  Instrument names render as-credited — the credit is often the more precise scholarly
claim (*violino piccolo*, *fortepiano*), and flattening it silently degrades (5.2) — while the MB-standard name serves as
the classification key (NORM-5; ONT-9).  The inversion is safe because no surface is keyed on rendered instrument strings;
the fragmentation hazard that forces canonical-first for artist names does not exist here.

**3.4 Work names follow the same identity logic.**  Canonical MB work names are the name-form authority at every hierarchy
level (NORM-6, NORM-7); per-release titles are evidence and terminal fallback only, and falling back is a basis change that
rides the annotation-completeness ladder (5.2).  Part names derive by stripping the parent-title prefix, with the
colon-space split guarding catalogue designations (NORM-9).  Per-release title text is never spliced into canonical name
strings (NORM-6).

**3.5 Sort forms.**  Sort names derive from the canonical form via its sort-name; as-credited variants carry no sort forms
of their own.  One sort key per entity is the anti-fragmentation rule applied to ordering surfaces.

**3.6 Derived temporal metadata.**  The composed date is canonical (1.7; SEL-16).  Period classification applies the
ratified period taxonomy (ONT-6) with its documented first-match convention over overlapping ranges (NORM-8); period is
reception metadata, revisable, never identity.

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

### Ontology

- **ONT-1 (adjudicated) — Instrument attribute inclusion (solo/guest/additional).**  The database's solo / guest /
  additional instrument attributes are excluded from rendered instrument names (the CE default, ratified).  The attributes
  remain selection *evidence* — a "solo" attribute feeds soloist confidence (1.2) — but are not rendering content.
- **ONT-2 (open) — Work-hierarchy scope: "part of collection" parents.**  Whether editorial collections enter the work
  hierarchy.  Direction from 1.7: compositional containers (a cycle, an opus-set as composed) belong to canonical identity;
  publisher and editorial collections do not describe what a work *is*.  CE includes collection parents by default, so a
  ruling against them is a documented divergence; full ruling with layers 2–3.
- **ONT-3 (adjudicated) — Partial recording identity.**  A partial recording is a recording-level fact about coverage of
  the work, rendered with a visible label; it does not mint a new work identity.  Ratifies the CE default; derives from 5.3
  (rendered, not buried).
- **ONT-4 (adjudicated) — Arrangement lineage as identity.**  The source work of an arrangement belongs to the
  arrangement's canonical identity — authorial lineage, 1.7.  Ratifies the CE default (the arranged-from work as a parent in
  the hierarchy).
- **ONT-5 (adjudicated) — Medleys.**  A medley's constituent works belong to its identity and are carried in the hierarchy
  with a visible label.  Ratifies the CE default; derives from 5.3.
- **ONT-6 (adjudicated) — Classical period taxonomy.**  The CE period map (Early through Contemporary) is ratified as the
  default period taxonomy.  Its overlapping ranges resolve first-match; the ordering dependency is a documented convention,
  not an error (interaction with normalisation adjudicated at layers 2–3).
- **ONT-7 (adjudicated) — Ensemble classification by name vocabulary.**  Classifying a performer as an ensemble by
  name-vocabulary matching (1.4) is ratified as the mechanical heuristic for the category.  Known edge case: substring
  matching without word boundaries misfires on compounds; implementations should prefer word-boundary or entity-type
  evidence where available.
- **ONT-8 (adjudicated) — Ensemble identification vocabulary.**  Consolidated with ONT-7: the enacted vocabulary
  (orchestras, choirs, chamber groups) is the concrete realisation of the ONT-7 heuristic.  Cross-referencing adjudication;
  IDs stable.
- **ONT-9 (adjudicated) — Vocal-keyword classification.**  Routing a soloist to the vocalist sub-class by voice-type
  keyword is ratified, with the "bass" ambiguity (voice vs. instrument) documented; explicit voice-type evidence is
  preferred where present (1.2).
- **ONT-10 (adjudicated) — Additional/assistant composer distinction.**  Completions and assistant authorship are a
  distinct authorial position (1.6), never silently merged into the primary composer.  Ratified.
- **ONT-11 (open) — Improvisational-primacy repertoire.**  The identity-authority inversion at the styleguide's boundary
  (1.7): where improvisation is primary — jazz above all — the audio capture is the authoritative record and performers are
  constitutive of the recorded work's identity.  Documented-open: outside the core domain; a future ruling owes the
  treatment of boundary repertoire (third-stream, notated jazz).

### Selection

- **SEL-1 (adjudicated) — Ambiguous soloist role.**  Albinoni's Adagio in G minor: neither the organ nor the violin
  obbligato is attributed as a soloist by default.  The parts are prominent, but prominence is not solohood, and
  era-sensitive traditional attribution does not name them (1.2) — in Albinoni's time the solo–ensemble spectrum was less
  focused than it later became.  The performers remain fully credited in full projections.  A specific release whose
  crediting establishes reasonable confidence (a celebrated organist billed as such) may attribute the soloist: the rule is
  confidence-based, not format-mechanical.  Derives from 1.2; P2, P3.
- **SEL-2 (adjudicated) — Concerto grosso.**  Concertino members are not soloists by default: Baroque-and-earlier practice
  predates the ossified solo–ensemble distinction, and traditional attribution names the ensemble (1.2, era sensitivity and
  ensemble-name precedence).  Exceptional engagement can establish soloists — four celebrated violinists in Vivaldi's
  Concerto for four violins.  Derives from 1.2; P2.
- **SEL-3 (adjudicated) — Independent choral ensemble.**  The chorusmaster occupies a distinct auxiliary position (1.5),
  attributed alongside the conductor in full projections — never as a conductor, never in compact projections.  The role is
  preparatory: the audience perceives the choir's preparation through the choir (1.1); the credit is real and always carried
  in full.  CE merges the chorusmaster into the `conductor` host tag annotated "(choirmaster)" — whether shared tag surfaces
  reproduce that convention is a layer-4 grammar question (flagged for the rendering layer: a semantic narrowing of a shared
  tag is a divergence to document).  Derives from 1.1, 1.5; P1.
- **SEL-4 (adjudicated) — Ensemble works with unique parts.**  Where a collective identity exists, ensemble-name precedence
  governs: the ensemble is attributed, members are credits.  Where none exists (named individuals recording the Messiaen
  Quatuor), the performers are chamber players — individually credited in full projections, soloist position empty:
  prominence is not solohood, and a title "for N soloists" does not mechanically confer the position.  Derives from 1.2.
- **SEL-5 (adjudicated) — Guest soloists within an ensemble.**  Guest status is credit metadata, not a position.  A
  performer occupies whatever position 1.2 assigns on the merits: a guest concerto soloist is a soloist by work format, not
  by guesthood; a guest covering an ensemble part remains ensemble-attributed, individually credited in full projections
  with the guest attribute preserved as evidence (ONT-1).  Exceptional engagement is the one lever by which guesthood
  itself raises soloist confidence.  Derives from 1.2, ONT-1.
- **SEL-6 (adjudicated) — Play-direct.**  A soloist directing from the instrument occupies both the soloist and conductor
  positions at once (1.1: positions, not a partition; 1.3).  Full projections carry both roles; compact projections render
  the performer once, at the soloist position — a contraction whose direction is itself the traditional billing.  Derives
  from 1.1, 1.3; P1.
- **SEL-7 (adjudicated) — Opera principals.**  Principal-role singers occupy the soloist position (vocalist sub-class) by
  the work-format criterion: opera constitutively names its solo roles, so the position is conferred exactly as in the
  concerto (1.2).  The principal–comprimario line follows reasonable confidence, with the release's own cast billing as the
  default descriptive evidence; comprimario and supporting singers are credited performers.  The compact ceiling a
  six-principal cast meets is a layer-4 inclusion-policy question — selection never pre-truncates to spare a surface.
  Derives from 1.2; P1, P2.
- **SEL-8 (adjudicated) — Completers and orchestrators.**  The mirror image of SEL-11: identity-bearing authorship of the
  performed edition — completion, orchestration, reconstruction — is part of the work's canonical identity (1.7, chain of
  authors) and enters both full and compact projections, role-annotated, primary composer always leading: Mahler 10 as
  performed is not identifiable without Cooke.  Incidental editorial work (critical editions, continuo realisations) is
  credited in full projections only.  Derives from 1.7, ONT-10.
- **SEL-9 (adjudicated) — Transcription chains.**  A transcription's authorial chain is its canonical identity, attributed
  source-composer-first — the traditional "Bach–Busoni" billing is itself source-first, so the descriptive and normative
  streams agree — with the transcriber role-annotated and the source work as hierarchy parent.  Longer chains carry in
  composition order.  Derives from 1.7, ONT-4.
- **SEL-10 (adjudicated) — Anonymous and traditional works.**  Split by which work is performed (1.7).  Where the performed
  work is the anonymous or traditional work itself, the composer position renders the anonymity honestly — "Anonymous",
  "Traditional" — as legitimate no-data (5.2): never invented, never filled by promoting an arranger or editor.  Where the
  performed work is an arrangement-work of traditional material, the arrangement is the work and its author chain
  terminates at the arranger, its terminal author, with the traditional source as parent (ONT-4).  Derives from 1.7, 5.2.
- **SEL-11 (adjudicated — overturned) — Canonical-soloist promotion.**  Overturned entirely: no performer role is part of
  a work's canonical identity (1.7).  A concerto release always carries its soloist in the full projections; nothing is
  promoted into compact projections — the question "when is promotion justified?" has the answer *never*, by rejection
  rather than generalisation.  Any enacted concerto-only path promotion is rejected by this ruling, and the concerto
  path-ordering question is moot with it (REND-16, absorbed with layer 4).  For improvisational-primacy repertoire the
  premise inverts — see ONT-11.  Derives from 1.7; P1.
- **SEL-12 (adjudicated) — Recording artist vs. track artist.**  Dissolved into the model: the fork exists only for
  platforms with a single `artist` slot to fight over.  The attribution model always selects both work-scope authors and
  recording-scope performers; what any single tag carries is a layer-4 grammar question (REND-1).  CE's merge default is
  platform machinery, not model semantics.  Derives from P1; cross-references REND-1.
- **SEL-13 (divergence) — Lyricist suppression when no vocal performers.**  CE suppresses the lyricist tag on recordings
  with no vocal performers; this styleguide overturns that default: the lyricist is work-scope authorship (1.6, 1.7), and
  the work has a lyricist regardless of whether a given performance sounds the text.  Full projections carry the credit
  unconditionally.  Documented divergence from the CE default.  Derives from 1.7; P1, P3.
- **SEL-14 (adjudicated) — Genre source selection.**  Work-type-derived genre is the primary editorial genre for art music
  — it derives from compositional identity (1.7).  Reception sources (folksonomy, file history) are admissible secondary
  evidence, never overriding work type where present; artist inference is excluded as basis-free (P3).  CE's multi-source
  machinery is platform capability, not styleguide semantics.
- **SEL-15 (adjudicated) — Classical classification scope.**  Selective, evidence-driven classification per release is
  ratified (the CE default); blanket classification is rejected.
- **SEL-16 (adjudicated) — Work date source selection.**  The composed date is canonical (1.7).  Published and premiered
  dates are legitimate secondary claims, usable as fallbacks with visible basis where the surface affords it (5.3 — the
  `[rec]`/`[rel]` pattern generalised to work dates).
- **SEL-17 (adjudicated) — Recording-level relation routing.**  The routing from source relation types to role buckets is
  ratified as *credit routing*, with a binding gloss: the mechanical buckets keep their established CE semantics as credit
  containers — the compatibility floor for every CE-named tag — but the layer-1 soloist position (1.2) is strictly narrower
  than the soloist buckets.  Any surface that projects a position must consume position selection under 1.2, never bucket
  contents (2.5).  Duplicate-relation suppression is ratified alongside as data hygiene.
- **SEL-18 (adjudicated) — Work-level relation routing and the writer asymmetry.**  The work-level routing is ratified,
  including the deliberate asymmetry: at work scope the writer/composer distinction is real and preserved (1.6); at
  recording scope `writer` merges into composers as CE compatibility floor (standing rule 1) — safe because the model
  retains the distinction at work scope and full projections can recover it.
- **SEL-19 (adjudicated) — Composer source priority.**  Work-level primary → work-level additional → recording-level:
  work-scope authorship is canonical identity (1.7) and outranks recording-scope credits; the recording-level fallback is a
  basis change handled per 5.2.
- **SEL-20 (adjudicated) — Primary work selection.**  When a recording performs several linked works, the primary work is
  the substantive composition, not a subsidiary artifact (cadenza collections being the proven case).  The enacted scoring
  heuristic (work type present; no backward derivation link) is ratified as a documented proxy for this preference —
  counterexamples revise the mechanism, not the principle.
- **SEL-21 (adjudicated) — Concerto grosso soloist sets.**  Consolidated with SEL-2: no concertino member is a soloist by
  default, so no individual-selection question arises by default; all credited concertino players remain full-projection
  credits with instruments.  Where SEL-2's exceptional-engagement carve-out fires, the soloists established are exactly
  those the engagement evidence names — never the whole concertino mechanically.
- **SEL-22 (adjudicated) — Vocal soloists in choral works.**  Named solo parts in choral works ("soprano solo", the
  Evangelist) confer the soloist position by work format (1.2), distinct from and never subsumed into the choir credit.
  Era sensitivity does not rebut constitutively named parts — that negative rule targets inference from prominence, and no
  inference is needed here.  The choir remains the ensemble; the chorusmaster remains SEL-3.

### Normalisation

- **NORM-1 (adjudicated) — Historical ensemble renames.**  One entity renders under one canonical (current) name in compact
  projections — identity-stability is the surface's purpose, and era-split directories are exactly the fragmentation defect
  the layer exists to prevent.  The era-correct credit is preserved as-credited in full projections (P3: the credit is part
  of the claim's basis).  The accepted cost is anachronism in the handle.  Contested by nature; carries its case-ID for 5.5
  marking.  Derives from 3.1, 3.2; P2, P3.
- **NORM-2 (adjudicated) — Native language and script.**  The canonical form is the entity's native name where Latin-script
  (*Wiener Philharmoniker*, never "Vienna Philharmonic"); non-Latin-script names render in their established Latin
  reception form (*Tchaikovsky*, *Shostakovich*) — the recognisable form, not a scholarly romanisation (P2) — with the
  native-script form available in full projections.  Ratifies CE's script-boundary instinct while rejecting anglicisation
  of native-Latin names.  Derives from 3.1; P2, P3.
- **NORM-3 (adjudicated) — Alias vs. MB-standard name-form.**  Aliases are evidence for choosing the one canonical form per
  entity (3.1), never a per-release replacement mechanism.  CE's per-context credited-as toggles are platform machinery;
  its conservative defaults for the recording and composer contexts point the same direction as 3.1's stability rule.
- **NORM-4 (adjudicated) — Alias vs. credited-as precedence.**  Dissolved by the two-slot model: full projections carry
  canonical *and* as-credited, so nothing competes.  Where a single-slot surface forces a choice, canonical wins — agreeing
  with the CE default.
- **NORM-5 (adjudicated) — Instrument name form.**  Instruments invert the artist-name rule: the as-credited instrument
  name renders (the credit is often the more precise scholarly claim — *violino piccolo*, *fortepiano* — and flattening it
  silently degrades, 5.2); the MB-standard name is the classification key (ONT-9).  Safe because no surface is keyed on
  rendered instrument strings.  Ratifies the CE default.  Derives from 3.3; 5.2.
- **NORM-6 (divergence) — Work name source.**  Canonical MB work names are the name-form authority; per-release titles are
  evidence and terminal fallback only, and the fallback is a basis change that rides the annotation-completeness ladder
  (5.2).  CE's "extended" style — per-release title text spliced into work names in braces — is rejected for canonical
  surfaces: a narrow documented divergence.  Derives from 3.4.
- **NORM-7 (adjudicated) — Work text resolution.**  Full hierarchy ratified: each level renders its own canonical name.
  Deriving all levels from level-0 text manufactures consistency the source does not claim.
- **NORM-8 (adjudicated) — Period map boundaries.**  The ratified period taxonomy (ONT-6) applies with its overlapping
  ranges and documented first-match resolution (1810 → Classical).  Period is reception metadata, not identity; the
  convention's arbitrariness at the margins is acceptable and revisable (P2).
- **NORM-9 (adjudicated) — Work-title prefix stripping.**  Parent-title prefix stripping with the colon-space split
  requirement is ratified: a deliberate guard against catalogue-designation false splits (Hob. III:31).
- **NORM-10 (adjudicated) — Ensemble name language selection.**  Consolidated with NORM-2: the ensemble instance of the
  native-Latin rule.  Cross-referencing adjudication; IDs stable.

### Rendering

- **REND-1 (open) — Composer in `ARTIST`.**  For classical recordings, does the `ARTIST` grammar lead with the composer
  (as several established house styles do) or carry performers only?  Releases genuinely disagree; P2 applies.

### Epistemic register

- **EPIST-1 (adjudicated — out of editorial scope) — Cache usage.**  An operational platform option with no styleguide
  semantics, with one binding gloss: caching must never produce silently stale claims — 5.2 governs, and the cache is an
  implementation detail under it.
- **EPIST-2 (adjudicated) — Alternate work-tag interoperability.**  An instance of alternate-source annotation: governed by
  5.1/5.2 and the annotation-completeness ladder (EPIST-7).  The specific product integration is a platform option.
- **EPIST-3 (adjudicated) — External reference database.**  As EPIST-2: an alternate-source basis under the ladder;
  otherwise operational.
- **EPIST-4 (adjudicated — out of editorial scope) — Conditional processing skip.**  Operational; no editorial content.
- **EPIST-5 (adjudicated — out of editorial scope) — Logging verbosity.**  Operational; no editorial content.
- **EPIST-6 (adjudicated) — Toolchain provenance persistence.**  The principle is ratified: the toolchain and rules applied
  are part of an annotation's basis and are persisted with the annotated unit — realised in the provenance sidecar
  (library partition), never as free-text or option dumps in tags.  CE's in-tag mechanism is its platform's honest
  capability difference, the same structure as 5.5's tag-only carve-out.  Derives from 5.1, 5.5.
- **EPIST-7 (adjudicated) — Annotation tier ladder.**  The five-rung ladder (full-verified → search-resolved → partial →
  alternate-source → source-tags-only) is ratified as the direct realisation of 5.1's annotation-completeness ladder.
- **EPIST-8 (adjudicated) — Provenance sidecar.**  The sidecar mechanism, monotonically upgradeable only, is ratified as
  the library-partition realisation of 5.1 and 5.5; monotonicity is 5.2 enforced structurally — degradation cannot be
  recorded as progress.
