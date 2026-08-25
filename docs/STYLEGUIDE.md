# The Editorial Styleguide for Annotating Recorded Art Music

This document is the editorial basis of music-annotator: the articulated set of principles from
which every attribution, annotation, naming, and rendering decision derives.  It is a *generative*
styleguide — authored from principle so that it consults well both for the cases enumerated here and
for cases not yet discovered — rather than an accumulated list of per-case rules.  It is a living
    document: cases are adjudicated into it over time, and every adjudication cites the principles
    it derives from.

## Purpose and standing

**Universality.**  The styleguide answers "how should recorded art music be tagged and organised?"
independently of any implementation.  It is realised by music-annotator directly (a standalone
annotator with full filesystem and provenance machinery) and is intended as the philosophical basis
of a successor to the Classical Extras Picard plugin ("CEv3") on the Picard v3 API.  music-annotator
will eventually serve both roles — tagging within and without Picard — from this one basis.

**Classical Extras continuity.**  Classical Extras (CE) is the anchor convention: it encodes a
coherent, field-tested stance on how art-music recordings should be described, and this styleguide
builds on that contract rather than reconstructing it.  Three standing rules govern the
relationship:

1. CE (and, where live conventions exist, Picard) tag semantics are the compatibility floor.  Shared
   tag names keep their established meanings.
2. Extensions are additive: new semantics always get new tag names, never a redefinition of a CE or
   Picard tag.  Fragmentation is *same name, different semantics*; additive extensions do not
   fragment.
3. Divergences from CE are permitted only with a documented rationale, recorded in this document's
   case register.

**Relationship to the public conventions spec.**  A public specification of the implemented tag set
and rendering rules is derived *from* this styleguide once conventions freeze; the styleguide is the
internal generative basis, the spec is its externalised projection.  The two are distinct documents
so that generative authoring here is not constrained by public-register prose obligations.

## Foundational principles

**P1 — Cross-surface coherence.**  Directory paths (compact), tags (full), and playlists (full) are
renderings of *one* attribution model — never independent rules that happen to agree.  Compact
projections are UX-ceiling-bounded and carry only the audible principals; full projections carry
complete credits.  A rendering may *omit* relative to the model; it may never *disagree* with it.
Composite tags are projections in exactly this sense: a tag such as `ARTIST` is a defined grammar
over the attribution model (ordered role classes, separators, inclusion policy), structurally
identical to a path component and differing only in ceiling and completeness.

**P2 — Generative neutrality.**  Publishers and editors legitimately differ; the same work is
attributed differently across releases, exactly as publishing houses' style guides differ.  Where
sources disagree, this styleguide takes as neutral a defensible position as it can; where it must
choose, the choice and its rationale are documented as a registered case.  The styleguide must
consult well for undecided cases: a rule that only restates its examples has failed this principle.

**P3 — Annotation as claim.**  An annotation is a claim, not a fact; the library records claim *and
basis*.  Annotation is scholarship under irreducibly incomplete and contradictory sources — the
histories and artifacts of composition and recording are incomplete, and error is inevitable.  The
styleguide therefore treats confidence, provenance, and known contestation as first-class content
(the epistemic register, layer 5), within a strict boundary: tags and their companions carry
*curation-grade* epistemics (what is claimed, on what basis, at what confidence);
*scholarship-grade* argument — evidence, disputation, correction of the world's sources — routes
upstream to the shared databases and the scholarly record, never into tags.  Biblioteconomy in the
library; scholarship in the commons.

## Architecture: five layers, one partition

Every rule in this styleguide belongs to one of five layers, each consuming the one above it:

| # | Layer | Question it answers | |---|-------|---------------------| | 1 | **Ontology** | What
entities and roles exist?  What is a work's canonical identity? | | 2 | **Selection** | Who is
attributed, at each scope (work / recording / release)? | | 3 | **Normalisation** | Which name-form
renders an identity? | | 4 | **Rendering** | How does each surface (path, each composite tag,
playlist) render the model? | | 5 | **Epistemic register** | With what confidence, provenance, and
contestation marking? |

Orthogonally, every rule lands in one of two **platform partitions**:

- **MB-derivable** — computable deterministically from MusicBrainz data plus explicit configuration:
  attribution selection, normalisation, composite-tag grammars, work-title policy.  This partition
  is implementable by a tag-only platform such as a Picard plugin and constitutes the
  CEv3-implementable surface.
- **Library-level** — requiring filesystem, provenance, or operator machinery: path construction,
  transaction journaling, confidence persistence, sidecars, playlists, physical-media attestation.
  Available to music-annotator standalone; not to a tag-only platform.

Cross-surface coherence (P1) governs both partitions from the one model; the partition determines
only where a rule can execute.  A rule in the MB-derivable partition must never quietly depend on
library-level machinery.

## Layer 1 — Ontology

The role taxonomy and the canonical-identity definition.  Authored from the adjudication of the
sharpest selection cases (SEL-1, SEL-2, SEL-6, SEL-11) rather than in the abstract; the rulings live
in the case register, the generalised taxonomy here.

**1.1 The performer spine.**  Recordings are attributed to three principal performer categories, in
the order **soloists → conductors → ensembles**, mirroring how visible and audible credits are
traditionally arranged — the film-credits stance: attribute the principals the audience perceives,
not every contributor.  The spine defines *positions*, not a partition: a performer may occupy more
than one position at once (play-direct, SEL-6), and any position may be empty or multiply occupied
(a concerto grosso has no default soloist, SEL-2; a triple concerto has three).

**1.2 The soloist position.**  A performer is a soloist **iff reasonable confidence establishes the
part as a named or attributive solo**.  Confidence draws on two independent, covariant streams: the
*descriptive* stream (the crediting history of the work and release — the source database's
conventions and editorial work, the recording label's, the publisher's and engraver's before that,
and every incidental force in the work's transmission) and the *normative* stream (the defensible,
wise optimum this styleguide strives for).  The two usually align; where they diverge, this
styleguide's judgment governs the rendered projections, and the release's own crediting survives as
evidence (P3: an annotation is a claim, and the credit is part of its basis).

Sources of reasonable confidence:

- **Work format.**  Formats that constitutively name the solo role attribute the soloist by
  construction: concerto, organ symphony, lied and song cycle, sonata-with-accompanist, and kin.
- **Traditional attribution.**  Where performance practice and reception history name the part, the
  position is supported even without a constitutive format.

Negative rules — each rebuts an expansive reading of the position:

- **Prominence is not solohood.**  Chamber players are not soloists however prominent their
  material, and independent musicians collaborating on a chamber recording remain chamber players.
  A contemporary work titled "for three soloists" does not mechanically confer the position — the
  title may itself be a theoretical exploration of the term.
- **Orchestral principals are not soloists.**  Principal string, wind, and brass players routinely
  render solo passages — the concertmaster in Scheherazade holds forth extensively — and are
  traditionally unnamed: the part belongs to the chair, not to a named guest, and it would be
  unusual for an orchestra to engage an external player for it.  Percussion is the reductio: under
  an expansive definition every percussionist would be a soloist.
- **Era sensitivity.**  Baroque and earlier repertoire predates the ossification of the
  solo–ensemble spectrum; concertino parts and obbligato lines (the concerto grosso; the Albinoni
  Adagio's organ and violin) do not confer soloist positions by default.  Exceptional engagement can
  establish them (four celebrated violinists engaged for Vivaldi's Concerto for four violins).
- **Ensemble-name precedence.**  Performers collectively known under an ensemble name are attributed
  as the ensemble, never as individuals.

Demotion from the soloist position never deletes a credit: an individually-credited non-soloist
remains a performer in the full projections, with instrument and as-credited form intact (P1 — a
rendering may omit, never disagree).  The soloist position is an editorial category over the
credits, not a container for them.

Within the position, sub-classification serves tag routing, not spine order: *vocalists* (voice-type
labels), *instrumentalists* (instrument labels), *other soloists* (no label).  The label vocabulary
is a known-imperfect heuristic — "bass" names both a voice and an instrument (ONT-9) — and explicit
voice-type evidence is preferred where present.

**1.3 Conductors and dual occupancy.**  The conductor position is occupied by performers credited as
conductor.  A play-direct performer — the soloist directing from the instrument — occupies both the
soloist and conductor positions at once (SEL-6): both roles are real, and full projections carry
both.  Compact projections render the performer once, at the soloist position; the contraction
toward the instrument is itself the traditional direction (a play-direct recording is billed under
the performer-at-the-instrument first).

**1.4 Ensembles.**  Orchestras, choirs, and chamber groups.  Classification is by collective
identity: a group known under an ensemble name is an ensemble, and its members are attributed
through it (ensemble-name precedence, 1.2).  Name-vocabulary matching (orchestra / philharmonic /
choir / quartet / …) is a serviceable mechanical heuristic for the category, with documented edge
cases (ONT-7).

**1.5 Auxiliary performer roles.**  Positions held in the taxonomy now even where their selection
rules wait for layer 2: **chorusmaster** (attribution alongside the conductor is SEL-3),
**concertmaster/leader**, **continuo**, **guest soloists within an ensemble** (SEL-5), **opera
principals** (SEL-7), and **vocal soloists in choral works** (SEL-22).  Adding a position later is
costlier than carrying one.

**1.6 Composer-side roles.**  Authorship positions, attributed at the work scope: **composer**;
**additional/assistant composer** (the usual database realisation of a completer — Süssmayr's
Requiem); **arranger**, **orchestrator**, **reconstructor**, **revisor**; **transcriber** (the
Bach–Busoni chain, SEL-9); **cadenza author**; **writer** — a distinct authorial position at the
work scope, though recording-scope practice has merged it into composer (the asymmetry is
adjudicated deliberate at SEL-18); **lyricist**, **librettist**, **translator**.  The positions are
fixed here; the selection rules — when a completer is attributed alongside the composer, whether the
completer enters compact projections (SEL-8) — are layer-2 rulings.

**1.7 Canonical identity of a work.**  A work's canonical identity is its **compositional
identity**: the properties fixed by the act and record of composition — title, key, catalogue and
opus designation, work type, compositional structure (movements, parts, a containing cycle),
compositional dates, and authorial lineage (the composer; for arrangements, completions, and
transcriptions, the chain of authors).  **No performer role is part of a work's canonical identity**
(SEL-11, adjudicated): a concerto is *for* a soloist but for no particular soloist; the soloist is a
property of a performance, never of the work.  The consequences are structural: compact projections
carry the work's compositional identity plus the performance's stable identity signals; nothing
performance-level is ever promoted into the work's identity.

**Scope boundary — improvisational primacy (ONT-11).**  The definition above holds for art music in
the written tradition, where the written record — and the scholarship between it and us — is the
provenance of the work; the improvisational era of classical performance is long over, its
improvisations remembered, recorded, and studied.  Forms with improvisational primacy — jazz above
all — invert the authority: the audio capture itself is the authoritative record, and the performers
are constitutive of the recorded work's identity.  Such repertoire sits at this styleguide's
boundary; the inversion is registered as ONT-11 (open) rather than forced into the written-tradition
model.

## Layer 2 — Selection

Who is attributed, per scope.  Selection operates at three scopes — work, recording, release — and
does two distinct jobs: admitting credits into the attribution model, and assigning performers to
the layer-1 positions.  The `SEL-*` rulings in the case register are the case law of these rules;
the rules generalise the rulings, not the reverse.

**2.1 Total selection, editorial positions.**  Every credit the sources carry is selected into the
model at its scope — selection never deletes.  Deletion upstream of rendering would make a surface's
omission indistinguishable from absence, breaking P1's guarantee that a rendering may omit but never
disagree.  The editorial act is position assignment: who occupies the spine positions (1.2–1.4) and
which auxiliary and authorial positions are occupied (1.5–1.6).  An individually-credited performer
who occupies no position is a credited performer — fully present in full projections, absent from
compact ones.

**2.2 Principals and support.**  The principals of a recording are the occupants of the spine
positions plus the canonical author chain (1.7).  Everything else — auxiliary position-holders
(chorusmaster, leader, continuo), credited non-principal performers, production credits — is
support.  Compact projections render principals only, subject to layer-4 ceilings; full projections
render everything.  The chorusmaster is the boundary exemplar (SEL-3): a real position, attributed
alongside the conductor in full projections, never compact-rendered, never merged into the conductor
position.

**2.3 Performer-side selection.**  The 1.2 confidence rule does all the work; the adjudicated cases
apply it:

- Named solo parts confer the position by work format: concerto soloists, opera principals (SEL-7),
  named vocal soloists in choral works (SEL-22).
- Collective identity precedes individual prominence: ensemble-name precedence for unique-part works
  (SEL-4); concertino members are not soloists by default (SEL-2, SEL-21).
- Guest status is credit metadata, not a position (SEL-5); the solo/guest/additional attributes are
  selection evidence only (ONT-1).
- The principal–comprimario line in staged works follows reasonable confidence, with the release's
  own billing as the default descriptive evidence (SEL-7).
- Performing bodies absent from release-level credits enter the ensemble position when present on a
  modal majority of the release's tracks (SEL-23); minority-track configurations remain credits
  only.

**2.4 Composer-side selection.**  The authorial chain of the performed edition is canonical (1.7)
and is always attributed: the primary composer leads; completers, orchestrators, and reconstructors
of the performing edition are attributed alongside, role-annotated, in compact as well as full
projections (SEL-8); transcription chains are attributed source-first (SEL-9).  Incidental editorial
work — critical editions, continuo realisations — is credited in full projections only.  Anonymity
is rendered honestly ("Anonymous", "Traditional"), never filled by promotion; where the performed
work is an arrangement-work, its author chain terminates at the arranger, its terminal author
(SEL-10).  The writer/composer distinction is preserved at work scope and merged at recording scope
as CE compatibility floor (SEL-18); composer surfaces prefer work-scope authorship (SEL-19).

**2.5 Credit routing is not position selection.**  The mechanical role buckets inherited from CE
(vocalists, instrumentalists, other soloists, ensembles, and kin) remain valid as credit containers
with their established CE semantics — the compatibility floor for every CE-named tag (SEL-17).  The
layer-1 soloist position (1.2) is strictly narrower than the soloist buckets.  Any surface that
projects a *position* — the path's performer components, any future position-semantic tag — must
consume position selection under 1.2, never bucket contents.

**2.6 Derived-metadata selection.**  Genre derives primarily from work type — compositional identity
(1.7) — with reception sources admissible as secondary evidence and artist inference excluded as
basis-free (SEL-14; P3).  Classical classification is selective and evidence-driven, never blanket
(SEL-15).  The composed date is the canonical work date; published and premiered dates are fallback
bases carried with visible basis where the surface affords it (SEL-16; 5.3).

## Layer 3 — Normalisation

Which name-form renders an identity.  The layer's founding problem is fragmentation: any surface
keyed on rendered forms scatters one entity across as many places as it has credit variants.

**3.1 One canonical form per entity.**  Every entity has exactly one canonical name-form, selected
once, not per release: the MB artist name field, verbatim — native script universally (*Wiener
Philharmoniker*, not "Vienna Philharmonic"; 小澤征爾, not "Seiji Ozawa"), with MB's own editorial
judgment supplying the fallback where a native form is unestablished, plural, or problematic
(NORM-2).  Aliases and credit variants are evidence for choosing this form, never per-release
replacement mechanisms (NORM-3; NORM-4 dissolves), and never a dereference target: a form-selection
rule that walks the alias list competes locales against each other and destabilises the canonical
form.

**3.2 Compact renders canonical; full preserves the credit.**  Compact projections render only
canonical forms, so one entity occupies one place.  Full projections render the canonical form and
carry as-credited variants wherever a release's credit differs — no credit information is lost (P3:
the credit is part of the claim's basis).  Entities whose names legitimately change over time render
under the current canonical name in compact projections; the era-correct credit survives as-credited
(NORM-1).  The accepted cost is anachronism in the handle; the rejected cost — one entity fragmented
into many places — would defeat the surface's purpose.

**3.3 Instruments invert the rule.**  Instrument names render as-credited — the credit is often the
more precise scholarly claim (*violino piccolo*, *fortepiano*), and flattening it silently degrades
(5.2) — while the MB-standard name serves as the classification key (NORM-5; ONT-9).  The inversion
is safe because no surface is keyed on rendered instrument strings; the fragmentation hazard that
forces canonical-first for artist names does not exist here.

**3.4 Work names follow the same identity logic.**  Canonical MB work names are the name-form
authority at every hierarchy level (NORM-6, NORM-7); per-release titles are evidence and terminal
fallback only, and falling back is a basis change that rides the annotation-completeness ladder
(5.2).  Part names derive by stripping the parent-title prefix, with the colon-space split guarding
catalogue designations (NORM-9).  Per-release title text is never spliced into canonical name
strings (NORM-6).

**3.5 Sort forms.**  Sort names derive from the canonical form via its sort-name; as-credited
variants carry no sort forms of their own.  One sort key per entity is the anti-fragmentation rule
applied to ordering surfaces.

**3.6 Derived temporal metadata.**  The composed date is canonical (1.7; SEL-16).  Period
classification applies the ratified period taxonomy (ONT-6) with its documented first-match
convention over overlapping ranges (NORM-8); period is reception metadata, revisable, never
identity.

## Layer 4 — Rendering

Per-surface grammars over the model: path components, each composite tag (`ARTIST`, `ALBUMARTIST`,
and the CE tag families), playlists.  The `REND-*` rulings in the case register are the case law of
these rules; the rules generalise the rulings, not the reverse.

**4.1 Every surface is a declared grammar.**  A rendering surface declares its content source (which
positions and credits it draws from the model), its ordering, its separators, and its inclusion
policy with its ceiling.  Two grammar registers exist, and the distinction is load-bearing: an
*assembled* surface projects the model editorially — its content is this styleguide's claim; a
*preserved* surface carries a source credit verbatim — its content is the release's claim, kept as
evidence (P3).  Both registers are projections of the one model (P1): a preserved credit is in the
model by total selection (2.1), so preservation can never disagree with it.

**4.2 Billing order.**  Assembled surfaces render performer principals in the spine's billing order
(1.1): soloists, then conductors, then ensembles.  Where a surface carries the author chain
alongside performers, the authors lead, in chain order (1.7, SEL-9): the work identifies before the
performance.  Preserved surfaces render as credited.  A grammar may deviate from billing order only
by an adjudicated case with documented rationale; none currently does (REND-14, REND-15).

**4.3 `ARTIST` and `ALBUMARTIST` are preserved claims.**  `ARTIST` renders the MB recording
artist-credit verbatim; `ALBUMARTIST` renders the release artist-credit verbatim (REND-1, REND-19).
The author chain is never spliced into `ARTIST`: authorship has its own surfaces (SEL-19), and
rendering a work-scope author in a performance-scope slot when performer data is absent would be a
silent scope substitution (5.2) — the mirror of SEL-10: as anonymity is never filled by promoting an
arranger, performer-lessness is never filled by promoting the composer.  The editorial performer
composite is a separate assembled tag — claim and adjudication remain distinct surfaces.

**4.4 CE-family tag grammars.**  CE-named tags keep their established CE semantics and grammar
(standing rule 1).  Role annotations render in parentheses within host tags using the CE annotation
vocabulary — "(orch.)", "(choirmaster)", "(arr.)", and kin (REND-3) — the visible-basis form of 5.3
applied to role merging: the merged credit carries its role in the claim itself.  `CONDUCTOR`
carries the conductor position plus the annotated chorusmaster credit (the grammar half of SEL-3:
credit routing per 2.5; the position ruling is untouched).  Multi-value lists within one tag join
with `"; "` (REND-17); the work-hierarchy family joins levels with `" :: "` (REND-6).  Which tag
names receive work, movement, genre, instrument, key, date, and period data is platform-configurable
with CE defaults (REND-5).  Format realisation — standard ID3 frames plus own-namespace TXXX
descriptors — is platform machinery under the same semantics (REND-13).

**4.5 The path grammar (library partition).**  The destination path is the compact assembled
projection: top directory (compilation, performer-led, and the dominant single-composer `<composer>
- <performers>` form — authors lead per 4.2, with `" - "` separating the author chain from
performers); work directory `<work title> [rec YYYY]`, the date basis visible in the rendered form
and `[rel YYYY]` as the labelled fallback (REND-24, 5.3); gap-free ordinal prefixes on intermediate
levels and leaves (REND-25, REND-26).  The first-component routing realises C-UNIVERSAL (which
superseded C-CLASS; the catalog path is prefix-less), described further at REND-22 and REND-23.  The
performers component carries the performance's stable identity signals — conductors, then ensembles,
billing order over its occupied positions.  Soloists never enter it, however principal (1.7,
SEL-11): this is the inclusion-policy answer to the ceiling question a six-principal opera cast
poses (SEL-7) — the handle stays stable because it never carries the credits that vary.  Two
converged rules govern the whole surface: **path is a handle, not a manifest** — a short, stable
identifier a user locates a recording by, never a manifest of contributors; and **uniform ceiling,
ragged floor** — over-resolved branches clamp down to the work-group's modal depth (removing
structure the path does not need — faithful), shallow branches are never padded up (inventing
structure that is not there — unfaithful).

**4.6 Playlists.**  Playlists are full projections: complete credits in billing order.  Their
detailed grammar is deferred to the adjudication loop until playlist machinery exists — an honest
gap, not an open contest.

**4.7 Title and classification surfaces.**  `ALBUM` renders the release title verbatim — a preserved
claim; composer prefixes are never spliced in (REND-2 — NORM-6's title-integrity logic at layer 4).
Work-scope surfaces render canonical work names (3.4), carrying key signatures contingently — only
where the canonical title lacks them (REND-10) — and coverage labels ("(part)", "Arrangement:",
"Medley") as visible marks (REND-7, 5.3).  Genre and classical classification render from
compositional identity (REND-20; SEL-14, SEL-15), with the CE flag vocabulary preserved (REND-21).

## Layer 5 — The epistemic register

The realisation of P3.  Four rules and a marking mechanism.

**5.1 Claim and basis.**  Every annotation the system renders is a claim backed by a recorded basis.
Two orthogonal confidence ladders realise this at coarse grain: an *identity-confidence* ladder (how
confidently a file matches the recording it is claimed to be) and an *annotation-completeness*
ladder (how completely a release could be annotated from available sources).  Both are persisted
with the annotated material, so degradation is always explicit: an entry annotated from partial
sources *says so*, permanently, in the unit itself.

**5.2 Never silently degrade.**  Deliberate degradation (ingesting at partial confidence,
substituting a fallback basis) is permitted only when persisted as a first-class fact.  The
discrimination underneath is *failure versus no-data*: "the source answered that no data exists" is
legitimate emptiness; "the data could not be determined" is an error, and an error is never rendered
as if it were emptiness.

**5.3 Rendered, not buried.**  Where a fallback basis is used and the surface can afford it, the
rendered form itself carries the basis.  The exemplar: a recording-session year renders as `[rec
1984]`, but when only a release year is available the label *changes form* to `[rel 2000]` — the
reader sees the basis of the claim in the claim itself, at zero side-channel cost, surviving every
copy and export.  Rendering decisions in layer 4 must consider a visible-basis form before reaching
for a side channel.  The principle yields to ceilings: where a visible mark would deface a compact
    surface (an asterisk in a path; an annotation inside `ARTIST`), the mark moves to the mechanism
    of 5.5.

**5.4 Identity honesty.**  Claims flow only along verified identity edges.  Where identity is
knowingly approximated — for example, annotating from a parallel release of the same recordings when
the exact pressing is absent from the source database — the approximation is a persisted fact, never
a silent substitution.  Upstream the same principle is a submission bar: data is contributed to the
shared databases only when attested against the correct entity, with physical media as the ground
truth where doubt exists.  Structural disagreements between local material and database records
(track-count mismatches, layout differences) are physical-world facts: the annotator surfaces and
defers to the operator; it never guesses.

**5.5 Contested-case marking.**  Where releases or editors legitimately disagree and this styleguide
has chosen a neutral default (P2), applying that default is itself an annotation-on-the-annotation.
The mechanism: every adjudicated case in the register below carries a stable case-ID;
implementations that maintain per-release provenance sidecars record the applied case-IDs there —
claim in the unit, prose in this document, nothing free-text in tags.  Tag-only platforms (the
MB-derivable partition) apply the same defaults without persisting the mark: an honest capability
difference, not a coherence break, because the default itself is deterministic from this document
either way.

## Case register

Each case is a fact pattern that has been observed (or is confidently expected) to be attributed or
rendered differently across releases or editors — proof that the answer is editorial, not
mechanical.  Cases carry a stable ID (`<LAYER>-<n>`: `ONT-`, `SEL-`, `NORM-`, `REND-`, `EPIST-`), a
status (**open** / **adjudicated** / **divergence** — the last meaning a documented departure from
CE or Picard convention), and, once adjudicated, the ruling with the principles it derives from.
The register is seed, not closure: new cases append.

### Ontology

- **ONT-1 (adjudicated) — Instrument attribute inclusion (solo/guest/additional).**  The database's
  solo / guest / additional instrument attributes are excluded from rendered instrument names (the
  CE default, ratified).  The attributes remain selection *evidence* — a "solo" attribute feeds
  soloist confidence (1.2) — but are not rendering content.
- **ONT-2 (open) — Work-hierarchy scope: "part of collection" parents.**  Whether editorial
  collections enter the work hierarchy.  Direction from 1.7: compositional containers (a cycle, an
  opus-set as composed) belong to canonical identity; publisher and editorial collections do not
  describe what a work *is*.  CE includes collection parents by default, so a ruling against them is
  a documented divergence; full ruling with layers 2–3.
- **ONT-3 (adjudicated) — Partial recording identity.**  A partial recording is a recording-level
  fact about coverage of the work, rendered with a visible label; it does not mint a new work
  identity.  Ratifies the CE default; derives from 5.3 (rendered, not buried).
- **ONT-4 (adjudicated) — Arrangement lineage as identity.**  The source work of an arrangement
  belongs to the arrangement's canonical identity — authorial lineage, 1.7.  Ratifies the CE default
  (the arranged-from work as a parent in the hierarchy).
- **ONT-5 (adjudicated) — Medleys.**  A medley's constituent works belong to its identity and are
  carried in the hierarchy with a visible label.  Ratifies the CE default; derives from 5.3.
- **ONT-6 (adjudicated) — Classical period taxonomy.**  The CE period map (Early through
  Contemporary) is ratified as the default period taxonomy.  Its overlapping ranges resolve
  first-match; the ordering dependency is a documented convention, not an error (interaction with
  normalisation adjudicated at layers 2–3).
- **ONT-7 (adjudicated) — Ensemble classification by name vocabulary.**  Classifying a performer as
  an ensemble by name-vocabulary matching (1.4) is ratified as the mechanical heuristic for the
  category.  Known edge case: substring matching without word boundaries misfires on compounds;
  implementations should prefer word-boundary or entity-type evidence where available.
- **ONT-8 (adjudicated) — Ensemble identification vocabulary.**  Consolidated with ONT-7: the
  enacted vocabulary (orchestras, choirs, chamber groups) is the concrete realisation of the ONT-7
  heuristic.  Cross-referencing adjudication; IDs stable.
- **ONT-9 (adjudicated) — Vocal-keyword classification.**  Routing a soloist to the vocalist
  sub-class by voice-type keyword is ratified, with the "bass" ambiguity (voice vs. instrument)
  documented; explicit voice-type evidence is preferred where present (1.2).
- **ONT-10 (adjudicated) — Additional/assistant composer distinction.**  Completions and assistant
  authorship are a distinct authorial position (1.6), never silently merged into the primary
  composer.  Ratified.
- **ONT-11 (open) — Improvisational-primacy repertoire.**  The identity-authority inversion at the
  styleguide's boundary (1.7): where improvisation is primary — jazz above all — the audio capture
  is the authoritative record and performers are constitutive of the recorded work's identity.
  Documented-open: outside the core domain; a future ruling owes the treatment of boundary
  repertoire (third-stream, notated jazz).

### Selection

- **SEL-1 (adjudicated) — Ambiguous soloist role.**  Albinoni's Adagio in G minor: neither the organ
  nor the violin obbligato is attributed as a soloist by default.  The parts are prominent, but
  prominence is not solohood, and era-sensitive traditional attribution does not name them (1.2) —
  in Albinoni's time the solo–ensemble spectrum was less focused than it later became.  The
  performers remain fully credited in full projections.  A specific release whose crediting
  establishes reasonable confidence (a celebrated organist billed as such) may attribute the
  soloist: the rule is confidence-based, not format-mechanical.  Derives from 1.2; P2, P3.
- **SEL-2 (adjudicated) — Concerto grosso.**  Concertino members are not soloists by default:
  Baroque-and-earlier practice predates the ossified solo–ensemble distinction, and traditional
  attribution names the ensemble (1.2, era sensitivity and ensemble-name precedence).  Exceptional
  engagement can establish soloists — four celebrated violinists in Vivaldi's Concerto for four
  violins.  Derives from 1.2; P2.
- **SEL-3 (adjudicated) — Independent choral ensemble.**  The chorusmaster occupies a distinct
  auxiliary position (1.5), attributed alongside the conductor in full projections — never as a
  conductor, never in compact projections.  The role is preparatory: the audience perceives the
  choir's preparation through the choir (1.1); the credit is real and always carried in full.  The
  shared-tag grammar is ruled at layer 4: `CONDUCTOR` carries the conductor position plus the
  annotated chorusmaster credit "(choirmaster)" — CE's convention adopted as credit routing (2.5,
  4.4); the position ruling here is untouched.  Derives from 1.1, 1.5; P1.
- **SEL-4 (adjudicated) — Ensemble works with unique parts.**  Where a collective identity exists,
  ensemble-name precedence governs: the ensemble is attributed, members are credits.  Where none
  exists (named individuals recording the Messiaen Quatuor), the performers are chamber players —
  individually credited in full projections, soloist position empty: prominence is not solohood, and
  a title "for N soloists" does not mechanically confer the position.  Derives from 1.2.
- **SEL-5 (adjudicated) — Guest soloists within an ensemble.**  Guest status is credit metadata, not
  a position.  A performer occupies whatever position 1.2 assigns on the merits: a guest concerto
  soloist is a soloist by work format, not by guesthood; a guest covering an ensemble part remains
  ensemble-attributed, individually credited in full projections with the guest attribute preserved
  as evidence (ONT-1).  Exceptional engagement is the one lever by which guesthood itself raises
  soloist confidence.  Derives from 1.2, ONT-1.
- **SEL-6 (adjudicated) — Play-direct.**  A soloist directing from the instrument occupies both the
  soloist and conductor positions at once (1.1: positions, not a partition; 1.3).  Full projections
  carry both roles; compact projections render the performer once, at the soloist position — a
  contraction whose direction is itself the traditional billing.  Derives from 1.1, 1.3; P1.
- **SEL-7 (adjudicated) — Opera principals.**  Principal-role singers occupy the soloist position
  (vocalist sub-class) by the work-format criterion: opera constitutively names its solo roles, so
  the position is conferred exactly as in the concerto (1.2).  The principal–comprimario line
  follows reasonable confidence, with the release's own cast billing as the default descriptive
  evidence; comprimario and supporting singers are credited performers.  The compact ceiling a
  six-principal cast meets is a layer-4 inclusion-policy question — selection never pre-truncates to
  spare a surface.  Derives from 1.2; P1, P2.
- **SEL-8 (adjudicated) — Completers and orchestrators.**  The mirror image of SEL-11:
  identity-bearing authorship of the performed edition — completion, orchestration, reconstruction —
  is part of the work's canonical identity (1.7, chain of authors) and enters both full and compact
  projections, role-annotated, primary composer always leading: Mahler 10 as performed is not
  identifiable without Cooke.  Incidental editorial work (critical editions, continuo realisations)
  is credited in full projections only.  Derives from 1.7, ONT-10.
- **SEL-9 (adjudicated) — Transcription chains.**  A transcription's authorial chain is its
  canonical identity, attributed source-composer-first — the traditional "Bach–Busoni" billing is
  itself source-first, so the descriptive and normative streams agree — with the transcriber
  role-annotated and the source work as hierarchy parent.  Longer chains carry in composition order.
  Derives from 1.7, ONT-4.
- **SEL-10 (adjudicated) — Anonymous and traditional works.**  Split by which work is performed
  (1.7).  Where the performed work is the anonymous or traditional work itself, the composer
  position renders the anonymity honestly — "Anonymous", "Traditional" — as legitimate no-data
  (5.2): never invented, never filled by promoting an arranger or editor.  Where the performed work
  is an arrangement-work of traditional material, the arrangement is the work and its author chain
  terminates at the arranger, its terminal author, with the traditional source as parent (ONT-4).
  Derives from 1.7, 5.2.
- **SEL-11 (adjudicated — overturned) — Canonical-soloist promotion.**  Overturned entirely: no
  performer role is part of a work's canonical identity (1.7).  A concerto release always carries
  its soloist in the full projections; nothing is promoted into compact projections — the question
  "when is promotion justified?" has the answer *never*, by rejection rather than generalisation.
  Any enacted concerto-only path promotion is rejected by this ruling, and the concerto
  path-ordering question is moot with it (REND-16, consolidated into this ruling).  For
  improvisational-primacy repertoire the premise inverts — see ONT-11.  Derives from 1.7; P1.
- **SEL-12 (adjudicated) — Recording artist vs. track artist.**  Dissolved into the model: the fork
  exists only for platforms with a single `artist` slot to fight over.  The attribution model always
  selects both work-scope authors and recording-scope performers; what any single tag carries is a
  layer-4 grammar question (REND-1).  CE's merge default is platform machinery, not model semantics.
  Derives from P1; cross-references REND-1.
- **SEL-13 (divergence) — Lyricist suppression when no vocal performers.**  CE suppresses the
  lyricist tag on recordings with no vocal performers; this styleguide overturns that default: the
  lyricist is work-scope authorship (1.6, 1.7), and the work has a lyricist regardless of whether a
  given performance sounds the text.  Full projections carry the credit unconditionally.  Documented
  divergence from the CE default.  Derives from 1.7; P1, P3.
- **SEL-14 (adjudicated) — Genre source selection.**  Work-type-derived genre is the primary
  editorial genre for art music — it derives from compositional identity (1.7).  Reception sources
  (folksonomy, file history) are admissible secondary evidence, never overriding work type where
  present; artist inference is excluded as basis-free (P3).  CE's multi-source machinery is platform
  capability, not styleguide semantics.
- **SEL-15 (adjudicated) — Classical classification scope.**  Selective, evidence-driven
  classification per release is ratified (the CE default); blanket classification is rejected.
- **SEL-16 (adjudicated) — Work date source selection.**  The composed date is canonical (1.7).
  Published and premiered dates are legitimate secondary claims, usable as fallbacks with visible
  basis where the surface affords it (5.3 — the `[rec]`/`[rel]` pattern generalised to work dates).
- **SEL-17 (adjudicated) — Recording-level relation routing.**  The routing from source relation
  types to role buckets is ratified as *credit routing*, with a binding gloss: the mechanical
  buckets keep their established CE semantics as credit containers — the compatibility floor for
  every CE-named tag — but the layer-1 soloist position (1.2) is strictly narrower than the soloist
  buckets.  Any surface that projects a position must consume position selection under 1.2, never
  bucket contents (2.5).  Duplicate-relation suppression is ratified alongside as data hygiene.
- **SEL-18 (adjudicated) — Work-level relation routing and the writer asymmetry.**  The work-level
  routing is ratified, including the deliberate asymmetry: at work scope the writer/composer
  distinction is real and preserved (1.6); at recording scope `writer` merges into composers as CE
  compatibility floor (standing rule 1) — safe because the model retains the distinction at work
  scope and full projections can recover it.
- **SEL-19 (adjudicated) — Composer source priority.**  Work-level primary → work-level additional →
  recording-level: work-scope authorship is canonical identity (1.7) and outranks recording-scope
  credits; the recording-level fallback is a basis change handled per 5.2.
- **SEL-20 (adjudicated) — Primary work selection.**  When a recording performs several linked
  works, the primary work is the substantive composition, not a subsidiary artifact (cadenza
  collections being the proven case).  The enacted scoring heuristic (work type present; no backward
  derivation link) is ratified as a documented proxy for this preference — counterexamples revise
  the mechanism, not the principle.
- **SEL-21 (adjudicated) — Concerto grosso soloist sets.**  Consolidated with SEL-2: no concertino
  member is a soloist by default, so no individual-selection question arises by default; all
  credited concertino players remain full-projection credits with instruments.  Where SEL-2's
  exceptional-engagement carve-out fires, the soloists established are exactly those the engagement
  evidence names — never the whole concertino mechanically.
- **SEL-22 (adjudicated) — Vocal soloists in choral works.**  Named solo parts in choral works
  ("soprano solo", the Evangelist) confer the soloist position by work format (1.2), distinct from
  and never subsumed into the choir credit.  Era sensitivity does not rebut constitutively named
  parts — that negative rule targets inference from prominence, and no inference is needed here.
  The choir remains the ensemble; the chorusmaster remains SEL-3.
- **SEL-23 (adjudicated) — Performing-body admission beyond release-level credits.**  The ensemble
  position at release scope admits the union of release-level-credited ensembles and bodies present
  on a modal majority (>50%) of the release's tracks.  Release-level credits alone demonstrably
  drop true performing bodies — a wind subgroup that is the release's actual performing body, a
  chorus credited per-track in a choral work — while the anti-forking property that motivated the
  release-level rule survives: minority-track configurations stay out, and no soloist enters
  regardless (SEL-11).  Bodies below the threshold remain full-projection credits (2.1).  The
  majority is computed over the release's full track set — the unit that shares a compact handle —
  identically at annotation and at any later recompute.  Derives from 1.4, 2.2, 2.3; P1.

### Normalisation

- **NORM-1 (adjudicated) — Historical ensemble renames.**  One entity renders under one canonical
  (current) name in compact projections — identity-stability is the surface's purpose, and era-split
  directories are exactly the fragmentation defect the layer exists to prevent.  The era-correct
  credit is preserved as-credited in full projections (P3: the credit is part of the claim's basis).
  The accepted cost is anachronism in the handle.  Contested by nature; carries its case-ID for 5.5
  marking.  Derives from 3.1, 3.2; P2, P3.
- **NORM-2 (adjudicated — revised 2026-08-24) — Native language and script.**  The canonical form
  is the MB artist name field, verbatim: native script universally (小澤征爾, Игорь Фёдорович
  Стравинский, *Wiener Philharmoniker* — never "Vienna Philharmonic"), as realised by MB's own
  naming practice.  Aliases are evidence only, never dereferenced for form selection (NORM-3) and
  never locale-competed: MB's primary flags are scoped per locale, so any first-primary rule selects
  an arbitrary locale and the canonical form loses its fixed point.  Where an entity's native form
  is unestablished, plural, or problematic, the fallback judgment is inherited from MB's editors
  through the same field (a Latin career name for an émigré artist) — never re-implemented locally
  (MB-authority deference: always a form MB asserts).  The original ruling's Latin-reception clause
  for non-Latin scripts (*Tchaikovsky*) is overturned: full-length native forms render even where a
  shorter reception form exists (the patronymic-full Стравинский is accepted).  Latin forms survive
  in sort-names (3.5) and in last-name path components, which derive from the Latin sort-name.
  Derives from 3.1; P2, P3.
- **NORM-3 (adjudicated) — Alias vs. MB-standard name-form.**  Aliases are evidence for choosing the
  one canonical form per entity (3.1), never a per-release replacement mechanism.  CE's per-context
  credited-as toggles are platform machinery; its conservative defaults for the recording and
  composer contexts point the same direction as 3.1's stability rule.
- **NORM-4 (adjudicated) — Alias vs. credited-as precedence.**  Dissolved by the two-slot model:
  full projections carry canonical *and* as-credited, so nothing competes.  Where a single-slot
  surface forces a choice, canonical wins — agreeing with the CE default.
- **NORM-5 (adjudicated) — Instrument name form.**  Instruments invert the artist-name rule: the
  as-credited instrument name renders (the credit is often the more precise scholarly claim —
  *violino piccolo*, *fortepiano* — and flattening it silently degrades, 5.2); the MB-standard name
  is the classification key (ONT-9).  Safe because no surface is keyed on rendered instrument
  strings.  Ratifies the CE default.  Derives from 3.3; 5.2.
- **NORM-6 (divergence) — Work name source.**  Canonical MB work names are the name-form authority;
  per-release titles are evidence and terminal fallback only, and the fallback is a basis change
  that rides the annotation-completeness ladder (5.2).  CE's "extended" style — per-release title
  text spliced into work names in braces — is rejected for canonical surfaces: a narrow documented
  divergence.  Derives from 3.4.
- **NORM-7 (adjudicated) — Work text resolution.**  Full hierarchy ratified: each level renders its
  own canonical name.  Deriving all levels from level-0 text manufactures consistency the source
  does not claim.
- **NORM-8 (adjudicated) — Period map boundaries.**  The ratified period taxonomy (ONT-6) applies
  with its overlapping ranges and documented first-match resolution (1810 → Classical).  Period is
  reception metadata, not identity; the convention's arbitrariness at the margins is acceptable and
  revisable (P2).
- **NORM-9 (adjudicated) — Work-title prefix stripping.**  Parent-title prefix stripping with the
  colon-space split requirement is ratified: a deliberate guard against catalogue-designation false
  splits (Hob. III:31).
- **NORM-10 (adjudicated) — Ensemble name language selection.**  Consolidated with NORM-2: the
  ensemble instance of the native-Latin rule.  Cross-referencing adjudication; IDs stable.

### Rendering

- **REND-1 (divergence) — Composer in `ARTIST`.**  `ARTIST` renders the recording's performance
  principals as the MB recording artist-credit verbatim — a preserved claim (4.3).  The author chain
  never enters: rendering a work-scope author in a performance-scope slot when performer data is
  absent is a silent scope substitution (5.2), and an empty performer credit is legitimate no-data,
  never a vacancy filled by promotion (the SEL-10 mirror).  Narrow documented divergence from CE,
  whose default cascade appends the composer as a conditional fallback; the composer-led house style
  is rejected as conflating scopes.  Derives from 4.3, 5.2, 1.7; P2, P3; cross-references SEL-12.
- **REND-2 (divergence) — Composer-last-name prefix on album title.**  Overturns the CE default:
  `ALBUM` renders the release's own title verbatim; composer text is never spliced in.  The release
  title is the release's claim (P3), and manufacturing a title the release does not bear is NORM-6's
  rejected splicing at layer 4.  Composer-first browsing is served by the path handle and the
  authorship surfaces.  Derives from 4.7, 3.4; P3.
- **REND-3 (adjudicated) — Role-annotation text within host tags.**  The CE annotation vocabulary
  ("(orch.)", "(choirmaster)", "(arr.)", "(reconstructed)", "(revised)", "(trans.)", and kin) is
  ratified: annotations render the role of a merged credit visibly in the claim itself — 5.3 applied
  to role merging.  Derives from 4.4, 5.3.
- **REND-4 (adjudicated) — Lyrics/notes splitting.**  Splitting a lyrics tag into album-common and
  track-unique notes is ratified as CE convention; platform capability, no further editorial
  content.
- **REND-5 (adjudicated) — Tag-name assignment.**  Which tag names receive work, movement, genre,
  instrument, key, date, and period data is a platform-configurable projection choice; the CE
  default names are ratified as the neutral defaults.  Consolidates REND-8, REND-9, and REND-11
  (genre/flag, instrument/key, and date/period tag names — the same fork per data family); the
  classical-flag value semantics are REND-21.  Derives from 4.4; P2.
- **REND-6 (adjudicated) — Work-hierarchy and movement-number separators.**  `" :: "` joins work
  levels in the work-hierarchy tag family (the CE `groupheading` convention); `"."` follows movement
  numbers; both ratified.  Consolidates REND-18 (the enacted `" :: "` evidence — same subject).
  Derives from 4.4.
- **REND-7 (adjudicated) — Coverage and lineage labels.**  The visible labels for partial recordings
  ("(part)"), arrangements ("Arrangement:"), and medleys ("Medley") are ratified: each renders a
  work-identity fact in the claim itself, realising ONT-3, ONT-4, and ONT-5 under 5.3.  Derives from
  4.7, 5.3.
- **REND-8 (adjudicated) — Genre and classical-flag tag names.**  Consolidated with REND-5 (tag-name
  assignment); flag value semantics at REND-21.  Cross-referencing adjudication; IDs stable.
- **REND-9 (adjudicated) — Instrument and key tag names.**  Consolidated with REND-5.
  Cross-referencing adjudication; IDs stable.
- **REND-10 (adjudicated) — Key signature inclusion in work name.**  Contingent inclusion ratified
  (the CE default): the key renders in the work name only where the canonical title lacks it — the
  key is compositional identity (1.7) and duplication adds no claim.  Derives from 4.7, 1.7.
- **REND-11 (adjudicated) — Work date and period tag names.**  Consolidated with REND-5.  Date-basis
  visibility is REND-24; period taxonomy is ONT-6/NORM-8.  Cross-referencing adjudication; IDs
  stable.
- **REND-12 (adjudicated — out of editorial scope) — Tag blanking and sort-tag population.**
  Pre-mapping blanking and overwrite policy are platform pipeline machinery with no styleguide
  semantics; sort forms are governed by 3.5.
- **REND-13 (adjudicated) — Performer sub-tag grammar and format realisation.**  The CE secondary
  performer surfaces (`soloists`, `band`, `involved people`, and kin) are ratified with their CE
  semantics; ID3 realisation uses standard frames plus own-namespace TXXX descriptors.  Derives from
  4.4.
- **REND-14 (divergence) — Assembled performer-composite order.**  Overturned in part: the editorial
  performer composite renders in billing order — soloists, then conductors, then ensembles (4.2) —
  replacing the CE assembly order (soloists, ensembles, conductors).  The verbatim-credit fallback
  when the model has no performers is ratified.  Documented divergence from the CE ordering
  convention; the enacted assembly is realigned by a post-v1 change.  Derives from 4.2, 1.1.
- **REND-15 (adjudicated) — Path performers ordering.**  Conductors before ensembles in the path
  component is ratified: it is billing order over the positions the path carries, soloists never
  entering by SEL-11.  What the mining census read as a path-vs-tag inversion dissolves — the
  deviant surface was the tag assembly (REND-14).  Derives from 4.2, 4.5.
- **REND-16 (adjudicated) — Concerto path soloist-first ordering.**  Consolidated into SEL-11: with
  canonical-soloist promotion overturned, no soloist enters the path and no concerto-specific
  ordering exists to rule.  Cross-referencing adjudication; IDs stable.
- **REND-17 (adjudicated) — Intra-list separator `"; "`.**  Ratified as the CE convention for
  multi-value lists within a single tag field and within path components.  Derives from 4.4.
- **REND-18 (adjudicated) — Work-hierarchy separator `" :: "`.**  Consolidated with REND-6.
  Cross-referencing adjudication; IDs stable.
- **REND-19 (adjudicated) — `ALBUMARTIST` source.**  The MB release artist-credit verbatim — a
  preserved claim (4.3).  Derives from 4.3; P3.
- **REND-20 (adjudicated) — `GENRE` source.**  Work-type-derived genre with a "Classical" default is
  ratified — the rendering realisation of SEL-14's work-type-primary selection.  Derives from 4.7;
  SEL-14.
- **REND-21 (adjudicated) — Classical flag.**  The CE flag vocabulary (`is_classical`, value `"1"`)
  is ratified for classical material.  The flag derives from the CE-classical work-type predicate
  (`cwp_work_top` non-empty AND `"Classical" in cwp_worktype_genres_top`) — compositional identity,
  not the code path.  The criterion "must derive from the classification, never from the code path"
  is satisfied.  Derives from 4.7; SEL-14, SEL-15.
- **REND-22 (C-CLASS refuted-and-deleted; superseded by C-UNIVERSAL) — Top-level class routing.**
  C-CLASS (a class-prefixed top-level directory) was refuted and deleted; the catalog path is now
  prefix-less under C-UNIVERSAL.  Editorial class distinctions (Classical, Popular, etc.) are
  relocated to the playlist lens, not the filesystem topology.  Derives from 4.5.
- **REND-23 (C-INIT absorbed and generalised into C-UNIVERSAL) — Within-classical top directory.**
  C-INIT's first-component rule is now universal: a pop album is the performer-led branch; a
  classical single-composer album is the composer-led branch.  The CE recital divergence remains:
  where MB links no composer, the album artist renders rather than an inferred composer — an
  inferred composer is a manufactured basis (5.2).  Derives from 4.5, 5.2.
- **REND-24 (adjudicated) — Work-directory year suffix.**  `[rec YYYY]` preferred, `[rel YYYY]` as
  the labelled fallback — the exemplar of 5.3: the basis of the date claim is visible in the
  rendered form, changing form when the basis changes.  Derives from 4.5, 5.3; SEL-16.
- **REND-25 (adjudicated) — Leaf ordinal chain.**  Movement number, then copy-subset index, then
  track position — ratified as deliberate ordinal machinery for a stable handle.  Derives from 4.5.
- **REND-26 (adjudicated) — Intermediate ordinal chain.**  Gap-free sibling rank from ordering keys,
  mirroring the leaf pattern — ratified.  Derives from 4.5.
- **REND-27 (adjudicated) — Author-chain rendering in the path composer component.**  The composer
  path component renders the canonical author chain plain, in chain order, primary composer leading
  (`Mozart; Süßmayr`).  Completers and kin enter compact projections per SEL-8, but their role
  annotations drop at the handle ceiling: 5.3 yields to ceilings, a "(compl.)" mark would deface a
  compact surface, so the annotation renders in tags (REND-3) and the applied-case mark rides the
  5.5 mechanism.  Derives from 4.5, 5.3; SEL-8.

### Epistemic register

- **EPIST-1 (adjudicated — out of editorial scope) — Cache usage.**  An operational platform option
  with no styleguide semantics, with one binding gloss: caching must never produce silently stale
  claims — 5.2 governs, and the cache is an implementation detail under it.
- **EPIST-2 (adjudicated) — Alternate work-tag interoperability.**  An instance of alternate-source
  annotation: governed by 5.1/5.2 and the annotation-completeness ladder (EPIST-7).  The specific
  product integration is a platform option.
- **EPIST-3 (adjudicated) — External reference database.**  As EPIST-2: an alternate-source basis
  under the ladder; otherwise operational.
- **EPIST-4 (adjudicated — out of editorial scope) — Conditional processing skip.**  Operational; no
  editorial content.
- **EPIST-5 (adjudicated — out of editorial scope) — Logging verbosity.**  Operational; no editorial
  content.
- **EPIST-6 (adjudicated) — Toolchain provenance persistence.**  The principle is ratified: the
  toolchain and rules applied are part of an annotation's basis and are persisted with the annotated
  unit — realised in the provenance sidecar (library partition), never as free-text or option dumps
  in tags.  CE's in-tag mechanism is its platform's honest capability difference, the same structure
  as 5.5's tag-only carve-out.  Derives from 5.1, 5.5.
- **EPIST-7 (adjudicated) — Annotation tier ladder.**  The five-rung ladder (full-verified →
  search-resolved → partial → alternate-source → source-tags-only) is ratified as the direct
  realisation of 5.1's annotation-completeness ladder.
- **EPIST-8 (adjudicated) — Provenance sidecar.**  The sidecar mechanism, monotonically upgradeable
  only, is ratified as the library-partition realisation of 5.1 and 5.5; monotonicity is 5.2
  enforced structurally — degradation cannot be recorded as progress.

## CE-divergence register

Standing rule 3: divergences from CE are permitted only with a documented rationale.  This register
is the enforcement artifact — every departure from a CE default or convention, with the ruling that
carries its rationale.  Additive extensions and platform capability differences are not divergences
(standing rule 2) and are footnoted separately.

- **SEL-13.**  CE suppresses the lyricist tag when no vocal performers are present; this styleguide
  carries the credit unconditionally — work-scope authorship survives instrumental performance.
- **NORM-2 (revised).**  CE's practice renders established Latin reception forms for
  non-Latin-script entities; the revised ruling renders the MB name field verbatim — native script
  universally, with fallbacks inherited from MB's own editorial judgment.
- **NORM-6.**  CE's "extended" style splices per-release title text into work names; rejected for
  canonical surfaces — titles are evidence and terminal fallback (3.4).
- **ONT-2 (pending).**  CE includes editorial collections in the work hierarchy; the direction here
  is compositional containers only (1.7).  Ruling documented-open — a pending divergence, not yet
  enforced.
- **REND-1.**  CE's `artist` cascade falls back to composers when performer sources are empty; here
  no composer ever enters `ARTIST` — the fallback is a scope substitution, and performer-lessness is
  legitimate no-data (5.2).
- **REND-2.**  CE prepends composer last names to the album title; `ALBUM` renders the release title
  verbatim — title integrity, NORM-6's logic at layer 4.
- **REND-14.**  CE orders assembled performer composites soloists, ensembles, conductors; billing
  order governs here — one ordering authority, the spine (1.1, 4.2).
- **REND-23.**  CE infers a composer-first form for recital directories; here the album artist
  renders when MB links no composer — an inferred composer is a manufactured basis (5.2).  The
  first-component rule is now universal under C-UNIVERSAL (C-INIT absorbed); the CE divergence on
  the performer-led branch remains.

**Capability differences (not divergences):** option/toolchain provenance persists in sidecars, not
in tags (EPIST-6 — the library-partition realisation of the same principle CE serves in-tag);
tag-only platforms apply contested-case defaults without persisting the mark (5.5 carve-out).

**Naming-drift remediation (resolved: no rename; verbatim semantics already live under
CEA_MB_ARTISTS/ARTIST):** the standing-rule-2 premise was imprecise — CE's `_cea_recording_artist`
means the *assembled* performer composite (census-ce.md:655), not the verbatim recording credit; the
verbatim credit is `_cea_MB_artists` → `CEA_MB_ARTISTS`, already correctly realised alongside
`ARTIST`.  The assembled composite stays under `CEA_RECORDING_ARTIST`; no rename, no new verbatim
tag.  Resolved at the S2 juncture (C-RA-GRAMMAR, 2026-07-31).

## Continuing styleguide development

- **the adjudication loop** (perpetual steady state): new cases append to the STYLEGUIDE register as
  annotation work surfaces them (C-CASE: IDs append-only, never renumbered or reused); statuses
  revise as evidence improves.  The census artifacts (`docs/census-ce.md`, `docs/census-impl.md`,
  `docs/census-library.md`) remain the arc's evidence reservoirs — STYLEGUIDE.md never cites them
  directly.
- **public conventions spec**: the externalised projection of the styleguide; identical to the
  library-completion arc's conventions-spec node, deliberately held until after the one-pass
  re-derivation so it describes final conventions.
- **CEv3**: the CE successor on Picard v3, platforming the styleguide's MB-derivable partition;
  graduates to its own roadmap if actioned (first step: contact the CE author).

## Postures and substrate facts to be consumed by future work

- **MB-authority deference (operator posture, 2026-08-11).**  Accept MB data as the source of
  authority even where fallible or incomplete; override it locally as defensibly and plainly as
  possible and only with well-defined automated transforms that yield significant improvements to
  library coherence or simplicity; do not introduce new conventions in annotation style or music
  scholarship.  The only editorial decisions are constrained to:
  - Selecting among MB's own asserted forms (e.g. its primary-flagged aliases) — never a local
    editorial table, never a form MB does not hold.  Belongs in foundational-principles register.
  - Deemphasizing non-scholarly or unpredictable data like genre.  Whereas fields like composer name
    or recording date are ultimately factual and thus all edits construed scholarship, fields like
    genre are ultimately subjective, are thus much more difficult to rubric, and attract divergent
    or chaotic values.
- **Alias-fetch substrate fact (2026-08-12).**  A canonical-form consumer must source aliases via
  the dedicated `fetch_artist_aliases(mbid)` (artist as the *direct* query target), never the
  `"aliases"` include on a release/recording fetch — the MB webservice does not reliably emit
  `<alias-list>` for artists nested in credits/relations.  Two enacted subtleties: the raw
  musicbrainzngs alias key is `"alias"`, not `"name"` (`MBAlias` remaps it); credit/relation artists
  off a release fetch carry `alias_list == []`, so consumers must hydrate via `fetch_artist_aliases`
  before calling `canonical_artist_form` or silently get the `name` fallback.
