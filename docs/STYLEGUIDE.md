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
within an ensemble** (SEL-5), **opera principals** (SEL-7), and **vocal soloists in choral works** (SEL-22, absorbed with
layers 2–3).  Adding a position later is costlier than carrying one.

**1.6 Composer-side roles.**  Authorship positions, attributed at the work scope: **composer**; **additional/assistant
composer** (the usual database realisation of a completer — Süssmayr's Requiem); **arranger**, **orchestrator**,
**reconstructor**, **revisor**; **transcriber** (the Bach–Busoni chain, SEL-9); **cadenza author**; **writer** — a distinct
authorial position at the work scope, though recording-scope practice has merged it into composer (the asymmetry is a
layer-2 adjudication); **lyricist**, **librettist**, **translator**.  The positions are fixed here; the selection rules —
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

## Layer 2 — Selection (to be authored)

Who is attributed, per scope.  All three spine categories are normally attributed; ambiguity lives in the edge cases, which
are adjudicated through the case register (`SEL-*`).  The layer owes: the principals-versus-support distinction and the
treatment of composer-side selection (when a completer or orchestrator is attributed alongside the composer).  The
canonical-soloist promotion question is closed at layer 1: no performer role enters a work's canonical identity (SEL-11).

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
- **SEL-3 (open) — Independent choral ensemble.**  Orchestra joined by a guest choir: is the chorusmaster attributed
  alongside the conductor?
- **SEL-4 (open) — Ensemble works with unique parts.**  Modern works written for named soloists, or where each performer
  plays a unique part, yet attribution conventionally goes to the ensemble.
- **SEL-5 (open) — Guest soloists within an ensemble.**  An ensemble joined by guests covering some (not all) solo parts:
  mixed individual-plus-ensemble attribution.
- **SEL-6 (adjudicated) — Play-direct.**  A soloist directing from the instrument occupies both the soloist and conductor
  positions at once (1.1: positions, not a partition; 1.3).  Full projections carry both roles; compact projections render
  the performer once, at the soloist position — a contraction whose direction is itself the traditional billing.  Derives
  from 1.1, 1.3; P1.
- **SEL-7 (open) — Opera principals.**  Are named-role singers "soloists"?  A principal cast of six meets the compact
  ceiling head-on.
- **SEL-8 (open) — Completers and orchestrators.**  Süssmayr's Requiem completion, Cooke's Mahler 10, Ravel's *Pictures*:
  composer credit, arranger credit, or both — and does the completer enter the compact projection?
- **SEL-9 (open) — Transcription chains.**  Bach–Busoni and kin: how does a transcription attribute its chain of authors?
- **SEL-10 (open) — Anonymous and traditional works.**  Selection when there is no composer to select.
- **SEL-11 (adjudicated — overturned) — Canonical-soloist promotion.**  Overturned entirely: no performer role is part of
  a work's canonical identity (1.7).  A concerto release always carries its soloist in the full projections; nothing is
  promoted into compact projections — the question "when is promotion justified?" has the answer *never*, by rejection
  rather than generalisation.  Any enacted concerto-only path promotion is rejected by this ruling, and the concerto
  path-ordering question is moot with it (REND-16, absorbed with layer 4).  For improvisational-primacy repertoire the
  premise inverts — see ONT-11.  Derives from 1.7; P1.

### Normalisation

- **NORM-1 (open) — Historical ensemble renames.**  One entity, era-dependent names: which form renders, and does the
  performance date select it?
- **NORM-2 (open) — Native language and script.**  Rendering names and titles for entities whose native form is not
  Latin-script, and titles whose authentic form differs from their reception-history form.

### Rendering

- **REND-1 (open) — Composer in `ARTIST`.**  For classical recordings, does the `ARTIST` grammar lead with the composer
  (as several established house styles do) or carry performers only?  Releases genuinely disagree; P2 applies.
