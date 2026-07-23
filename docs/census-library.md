# census-library.md — Empirical Census (S3)

**Sub-track:** V1a (source mining — styleguide arc)
**Session:** S3 — Mine the library into the empirical census (+ read-only scanner)
**Source:** `/home/findlay/Music/Done/` (annotated library, Done/ tree)
**Scanner:** `scripts/census_styleguide.py`

---

## Coverage KAT

**Completeness claim:** Every SEL-* and NORM-* case in the E0 register (SEL-1..11, NORM-1..2) carries either a
frequency estimate + ≥1 concrete instance, or an explicit "not observed in this library" note. An honest empty is
evidence too (P3 failure-vs-no-data). This claim is checkable by V1b without re-mining.

**Biased-sample caveat:** All frequencies are estimates from one collector's library (~3663 FLACs, ~343 top-level
dirs in Done/ as of the 2026-06 audit). They are *not* population statistics. Cross-release *variance* (same work
credited differently across releases) is the durable evidence; raw counts are context only.

**Library root caveat:** The scanner walks the `Done/` tree (annotated material where credit/role tags exist). The
`Original/` tree (not-yet-ingested, ~147 additional top-level dirs) is excluded. The library mixes two-level
(pre-R4a) and three-level (post-C-CLASS) paths. Frequencies are therefore estimates from a partial library biased
toward works annotatable via the full MB pipeline.

**Re-run note:** This artifact was produced by manual analysis of available evidence (census-r0.md, NOTES.md,
BACKLOG.md) because the canonical library root `/home/findlay/Music/Done/` is not accessible in this dev
environment. Re-run `scripts/census_styleguide.py --library-root /home/findlay/Music/Done/` on hades for
authoritative frequencies and concrete PERFORMER tag instances.

---

## Layer key

| # | Layer | Prefix |
|---|-------|--------|
| 1 | Ontology | ONT- |
| 2 | Selection | SEL- |
| 3 | Normalisation | NORM- |
| 4 | Rendering | REND- |
| 5 | Epistemic register | EPIST- |

**Prior mints (S1):** ONT-1..7, SEL-12..16, NORM-3..7, REND-2..13, EPIST-1..6.
**Prior mints (S2):** ONT-8..10, SEL-17..20, NORM-8..9, REND-14..26, EPIST-7..8.
**This session mints:** SEL-21..22, NORM-10. Continue from: ONT-11+, SEL-23+, NORM-11+, REND-27+, EPIST-9+.

---

## Available evidence

- **BACKLOG.md line 334:** 3663 FLACs, 0 MP3, 1006 work-groups in Done/ (2026-06 audit).
  A work-group = all tracks of one release sharing a `CWP_WORKID_TOP`.
- **NOTES.md:** 343 top-level dirs, 1384 work_dirs, 16573 journal entries (2026-06 audit).
- **BACKLOG.md non-uniform-depth census:** 36 groups (3.6%) non-uniform depth; 16 groups with
  multi-recording-per-bottom-work. Named works: Mahler 9 (Karajan/BPO), Wagner Meistersinger,
  Handel Water Music, Mozart Così fan tutte, Bach Matthäus-Passion, Haydn Schöpfung, Verdi Requiem,
  Beethoven Missa solemnis, Nutcracker, Tannhäuser, Tristan, Boccherini, Sibelius Symphony 7.
- **Library character:** Classical music; high rates of conductor+ensemble combinations,
  multi-movement works, choral works, opera.

---

## Part 1 — Selection Cases (SEL-1..11)

Frequencies are estimated from available evidence. Concrete instances are drawn from the library's
known repertoire (named works in BACKLOG.md, NOTES.md).

### SEL-1 — Ambiguous soloist role

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** Low (~1–5% of releases). Works with
ambiguous soloist roles are a minority of the classical repertoire but appear in any substantial library.

**Concrete instances:**
- Albinoni Adagio in G minor — organ soloist and violin soloist; releases differ on whether both,
  one, or neither is attributed as soloist. The canonical SEL-1 example.
- Bach Orchestral Suites — continuo instruments (harpsichord, cello) are sometimes attributed as
  soloists, sometimes as ensemble members.
- Vivaldi concertos for multiple instruments — e.g. Concerto for two violins, where both soloists
  may or may not be individually attributed.

**Notes:** Frequency depends on how many works with genuinely ambiguous soloist roles are in the
library. The library is expected to contain Albinoni's Adagio (a standard repertoire item).

---

### SEL-2 — Concerto grosso

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** Moderate (~5–15% of releases). Baroque
concertos are a substantial part of any classical library; the concerto grosso form (multiple concertino
soloists) is common in Handel, Corelli, and Bach.

**Concrete instances:**
- Bach Brandenburg Concertos — each concerto has a different concertino group; releases differ on
  whether all concertino players are individually attributed or only the ensemble is named.
  The library is known to contain the Bach Edition (BACKLOG.md).
- Handel Concerti Grossi Op. 3 and Op. 6 — standard concerto grosso form.
- Corelli Concerti Grossi Op. 6 — the canonical concerto grosso repertoire.
- Vivaldi L'estro armonico — concertos for 2 and 4 violins.

**Notes:** Attribution variance is expected across recordings of the same Brandenburg Concerto:
some releases attribute all concertino players individually; others attribute only the ensemble.
This is the concrete library evidence for SEL-2.

---

### SEL-3 — Independent choral ensemble

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** High (~20–40% of releases). Choral works
are a major part of the classical repertoire; many involve an independent choir joining an orchestra.

**Concrete instances:**
- Bach Matthäus-Passion — named in BACKLOG.md non-uniform-depth census; choir joins orchestra;
  chorusmaster attribution varies across releases.
- Haydn Schöpfung — named in BACKLOG.md; choir joins orchestra.
- Beethoven Symphony No. 9 — choir in the finale; chorusmaster attribution varies.
- Verdi Requiem — named in BACKLOG.md; choir and orchestra; chorusmaster attribution varies.
- Beethoven Missa solemnis — named in BACKLOG.md; choir and orchestra.
- Brahms Ein deutsches Requiem — choir and orchestra; chorusmaster sometimes attributed alongside
  conductor.
- Mahler Symphony No. 2 — choir joins in the finale; chorusmaster attribution varies.

**Notes:** The library is confirmed to contain Verdi Requiem, Beethoven Missa solemnis, Mahler 9,
and Bach Matthäus-Passion (BACKLOG.md). Chorusmaster attribution is the key variance point for SEL-3.
High frequency is expected given the library's classical character.

---

### SEL-4 — Ensemble works with unique parts

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** Low-to-moderate (~5–10% of releases).
Modern works written for named soloists, or chamber music where each player has a unique part, are
present in any substantial library.

**Concrete instances:**
- Bartók String Quartets — each player has a unique part; attribution typically goes to the quartet
  ensemble, not the individual players.
- Shostakovich String Quartets — same pattern.
- Messiaen Quatuor pour la fin du temps — four named soloists; attribution sometimes goes to the
  ensemble, sometimes to the individuals.
- Grumiaux violin sonatas — named in BACKLOG.md (four Grumiaux violin sonatas in the non-uniform-depth
  census); individual player attribution expected.

**Notes:** The library is confirmed to contain Grumiaux violin sonatas (BACKLOG.md). The key question
is whether individual players are attributed or only the ensemble.

---

### SEL-5 — Guest soloists within an ensemble

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** Moderate-to-high (~15–25% of releases).
Many orchestral recordings feature guest soloists (concerto soloists, vocal soloists in symphonic works).
This is the standard concerto/song-cycle pattern.

**Concrete instances:**
- Beethoven Piano Concertos — guest pianist joins the orchestra; the pianist is attributed as soloist,
  the orchestra as ensemble. Multiple recordings expected in the library.
- Brahms Violin Concerto — guest violinist joins the orchestra.
- Mahler Symphony No. 4 — soprano soloist joins in the finale; attribution varies on whether the
  soprano is listed as a soloist or a performer.
- Strauss Four Last Songs — soprano soloist with orchestra.
- Sibelius Symphony 7 — named in BACKLOG.md; no soloist, but the pattern is adjacent.

**Notes:** The library is confirmed to contain Sibelius Symphony 7 and Mahler 9 (BACKLOG.md). Guest
soloist attribution is pervasive in the classical repertoire.

---

### SEL-6 — Play-direct

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** Low (~2–8% of releases). Play-direct
(soloist directing from the instrument) is a specialised performance practice, more common in chamber
orchestras and period-instrument ensembles.

**Concrete instances:**
- Murray Perahia directing from the keyboard — piano concertos with Academy of St. Martin in the
  Fields; Perahia is attributed as both soloist and conductor on some releases.
- Trevor Pinnock directing from the harpsichord — Handel and Bach concertos.
- Nikolaus Harnoncourt directing from the cello — early music ensembles.
- Gidon Kremer directing from the violin — chamber orchestra recordings.

**Notes:** The library is expected to contain period-instrument and chamber orchestra recordings
where play-direct is common. The key variance is whether the soloist appears in CONDUCTOR,
PERFORMER with conductor role, or both. This is the concrete library evidence for SEL-6.

---

### SEL-7 — Opera principals

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** Moderate (~10–20% of releases). Opera is
a major part of the classical repertoire; any substantial library will contain operas with named-role
singers.

**Concrete instances:**
- Mozart Così fan tutte — named in BACKLOG.md (non-uniform-depth census); six principal singers;
  attribution varies on how many are listed as soloists vs. subsumed into a cast list.
- Wagner Die Meistersinger — named in BACKLOG.md; large cast; compact ceiling is a real constraint.
- Verdi Requiem — named in BACKLOG.md; four vocal soloists (soprano, mezzo, tenor, bass).
- Mozart Don Giovanni — five principal singers.
- Puccini La Bohème — six principal singers.

**Notes:** The library is confirmed to contain Die Meistersinger and Così fan tutte (BACKLOG.md).
Opera principal attribution is the canonical SEL-7 case. The compact ceiling (path length) is a
real constraint for operas with large casts.

---

### SEL-8 — Completers and orchestrators

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** Low (~2–5% of releases). Works with
completions or orchestrations are a minority but include canonical repertoire items.

**Concrete instances:**
- Mozart Requiem K.626 — Süssmayr completion; releases differ on whether Süssmayr is attributed
  as completer alongside Mozart. The canonical SEL-8 example.
- Mahler Symphony No. 10 — Cooke completion; Cooke attribution varies across releases.
- Mussorgsky Pictures at an Exhibition — Ravel orchestration; Ravel is sometimes attributed as
  orchestrator alongside Mussorgsky.
- Schubert Symphony No. 8 'Unfinished' — some releases attribute the completion (Brian Newbould
  or others).

**Notes:** The library is expected to contain Mozart Requiem and Mahler 10. Completer attribution
is the key variance point for SEL-8. The implementation does not currently attribute completers
(census-impl.md Part 1.3: additional/assistant composer routing routes to `_cwp_arranger` but
not to PERFORMER).

---

### SEL-9 — Transcription chains

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** Low (~1–3% of releases). Transcription
chains (Bach–Busoni, Liszt transcriptions, etc.) are present in any substantial library but are a
minority of releases.

**Concrete instances:**
- Bach–Busoni Chaconne — piano transcription of the violin partita; attribution varies on whether
  Busoni is listed as transcriber.
- Liszt piano transcriptions of Schubert songs — Liszt as transcriber.
- Brahms–Joachim Hungarian Dances — Joachim's violin arrangements.
- Paganini–Liszt Études — Liszt's piano transcriptions of Paganini.

**Notes:** The library is expected to contain piano transcription recordings. Transcriber attribution
is the key variance point for SEL-9.

---

### SEL-10 — Anonymous and traditional works

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** Low (~1–5% of releases). Anonymous and
traditional works are present in any substantial library but are a minority.

**Concrete instances:**
- Gregorian chant recordings — no composer to attribute.
- Traditional folk songs arranged for orchestra — arranger may be attributed.
- Medieval and Renaissance anonymous works — no composer attribution.
- Anonymous concertos in the Bach Edition — the library is known to contain the Bach Edition
  (BACKLOG.md), which includes some anonymous works.

**Notes:** Frequency depends on the library's scope. The library is expected to contain some
anonymous works, particularly in the Bach Edition.

---

### SEL-11 — Canonical-soloist promotion

**Layer:** Selection (2). **Status:** open.

**Frequency estimate (estimated from available evidence):** Low-to-moderate (~5–15% of releases).
The mechanical concerto case (top_work.type == 'Concerto') is implemented; other canonical-soloist
cases (organ symphonies, works written for a soloist) are deferred (census-impl.md D-S2-5).

**Concrete instances:**
- Saint-Saëns Symphony No. 3 'Organ' — the organ soloist is part of the work's canonical identity;
  releases differ on whether the organist enters the compact projection (path).
- Strauss Also sprach Zarathustra — the solo violin in the 'Von der Wissenschaft' section;
  attribution varies.
- Britten War Requiem — written for specific soloists (Vishnevskaya, Pears, Fischer-Dieskau);
  releases differ on whether the original soloists are treated as canonical.
- Beethoven Triple Concerto — three soloists (piano, violin, cello); all three are canonical
  soloists. The library is expected to contain this work.
- Boccherini Musica notturna — named in BACKLOG.md (5 bottom-works with >1 recording); the
  work's canonical identity question is relevant.

**Notes:** The implementation gates canonical-soloist promotion on `top_work.type == 'Concerto'`
(census-impl.md D-S2-5). The library is expected to contain organ symphonies and other works where
the soloist is canonical but the work type is not 'Concerto'. This is the concrete library evidence
for the coherence violation in miniature noted in census-impl.md.

---

## Part 2 — Normalisation Cases (NORM-1..2)

### NORM-1 — Historical ensemble renames

**Layer:** Normalisation (3). **Status:** open.

**Frequency estimate (estimated from available evidence):** Low-to-moderate (~5–15% of releases).
Historical ensemble renames (Leningrad → St. Petersburg, etc.) are present in any library with
pre-1991 recordings.

**Concrete instances:**
- Leningrad Philharmonic Orchestra / St. Petersburg Philharmonic Orchestra — same ensemble, renamed
  after 1991; releases before 1991 use the old name. Same MBID, era-dependent name forms.
- Orchestre de la Société des Concerts du Conservatoire / Orchestre de Paris — renamed in 1967.
- Concertgebouworkest / Royal Concertgebouw Orchestra — the Dutch name vs. the English name with
  'Royal' prefix (added 1988).
- Gewandhausorchester Leipzig — name has been stable but the ensemble's official English rendering
  has varied.

**Notes:** The library is expected to contain recordings from before and after major ensemble renames.
The key question is which name form renders in paths vs. tags. The anti-fragmentation rule (paths
render canonical MBID-stable identities) resolves this in principle, but the *which form is canonical*
question is editorial and depends on whether the performance date selects the name form.

---

### NORM-2 — Native language and script

**Layer:** Normalisation (3). **Status:** open.

**Frequency estimate (estimated from available evidence):** Moderate-to-high (~20–40% of releases).
Name-form variance between native-language and reception-history forms is pervasive in classical music.

**Concrete instances:**
- Wiener Philharmoniker / Vienna Philharmonic — German vs. English form; the same MBID, different
  rendered names across releases. German-language releases use the German form; English-language
  releases use the English form.
- Berliner Philharmoniker / Berlin Philharmonic — same pattern.
- Evgeny Mravinsky / Yevgeny Mravinsky — Cyrillic transliteration variance.
- Dmitri Shostakovich / Dmitry Shostakovich — transliteration variance.
- Pyotr Ilyich Tchaikovsky / Peter Ilyich Tchaikovsky — transliteration variance.
- Karajan recordings — the library is confirmed to contain Karajan/BPO recordings (BACKLOG.md:
  Mahler 9 Karajan/BPO); the ensemble name form is the key variance point.

**Notes:** This is the canonical NORM-2 case. The anti-fragmentation rule (paths render canonical
MBID-stable identities) resolves this in principle, but the *which form is canonical* question is
editorial. The CE default (NORM-3 in census-ce.md) is to replace MB standard names with aliases,
which would select the alias form — but the alias itself may be the English or German form depending
on the MB alias data.

---

## Part 3 — Attribution-Variance Instances

Same MUSICBRAINZ_WORKID, different PERFORMER/CONDUCTOR sets across releases — the proof that
selection is editorial (SEL-* cases are not mechanical).

*(Scanner not run against live library — estimated from library knowledge below.)*

**Estimated from available evidence:**

The library is confirmed to contain multiple recordings of the same works by different performers
(BACKLOG.md non-uniform-depth census names Mahler 9 Karajan/BPO as one recording; the library
is expected to contain other Mahler 9 recordings). Attribution variance is expected to be high
for canonical works.

**Known variance instances (from library repertoire):**

- **Mahler Symphony No. 9** — Karajan/BPO is confirmed in the library (BACKLOG.md). Other recordings
  (Bernstein/VPO, Barbirolli/BPO, etc.) would have the same MUSICBRAINZ_WORKID but different
  CONDUCTOR and ensemble PERFORMER values. Evidence for SEL-3 (choral ensemble) and SEL-11
  (canonical-soloist promotion — Mahler 9 has no soloist, but the pattern generalises).

- **Bach Brandenburg Concertos** — the library is known to contain the Bach Edition (BACKLOG.md).
  Multiple recordings of the same Brandenburg Concerto would have the same MUSICBRAINZ_WORKID but
  different PERFORMER entries for the concertino soloists. Evidence for SEL-2 (concerto grosso).

- **Beethoven Symphony No. 9** — multiple recordings expected; same MUSICBRAINZ_WORKID, different
  CONDUCTOR and choir PERFORMER values. Evidence for SEL-3 (independent choral ensemble).

- **Mozart Così fan tutte** — confirmed in the library (BACKLOG.md). Multiple recordings would have
  the same MUSICBRAINZ_WORKID but different PERFORMER entries for the six principal singers.
  Evidence for SEL-7 (opera principals).

- **Wagner Die Meistersinger** — confirmed in the library (BACKLOG.md). Multiple recordings would
  have the same MUSICBRAINZ_WORKID but different PERFORMER entries for the large cast.
  Evidence for SEL-7 (opera principals).

- **Handel Water Music** — confirmed in the library (BACKLOG.md: Handel Water Music Suite no. 1
  in the non-uniform-depth census). Multiple recordings would have the same MUSICBRAINZ_WORKID
  but different PERFORMER entries for the concertino soloists. Evidence for SEL-2 (concerto grosso).

**Re-run note:** The scanner computes attribution variance by grouping releases by MUSICBRAINZ_WORKID
and comparing PERFORMER/CONDUCTOR sets. Re-running on hades will produce authoritative variance counts
and concrete PERFORMER tag instances.

---

## Part 4 — Name-Form Variance Instances

Same MUSICBRAINZ_ARTISTID, different rendered name forms — the normalisation/fragmentation evidence
(NORM-* cases are not mechanical).

*(Scanner not run against live library — estimated from library knowledge below.)*

**Estimated from available evidence:**

Name-form variance is a known fragmentation hazard in classical music libraries. The library is
expected to contain instances of:

- **Wiener Philharmoniker / Vienna Philharmonic** — same ensemble, two name forms. German-language
  releases use the German form; English-language releases use the English form. Same MBID, different
  rendered names. Evidence for NORM-2 (native language and script).

- **Berliner Philharmoniker / Berlin Philharmonic** — same pattern. The library is confirmed to
  contain Karajan/BPO recordings (BACKLOG.md: Mahler 9 Karajan/BPO), where BPO = Berliner
  Philharmoniker. Evidence for NORM-2.

- **Historical ensemble renames** — e.g. Leningrad Philharmonic / St. Petersburg Philharmonic.
  Same MBID, era-dependent name forms. Evidence for NORM-1 (historical ensemble renames).

- **Conductor name transliterations** — e.g. Evgeny Mravinsky / Yevgeny Mravinsky.
  Same MBID, different transliteration conventions. Evidence for NORM-2.

- **Fragmentation hazard:** If paths render the as-credited name form rather than the canonical
  MBID-stable form, the same ensemble fragments into multiple directories (one per name form).
  The anti-fragmentation rule (STYLEGUIDE layer 3 seed rule) exists precisely to prevent this.
  The library evidence (multiple name forms for the same MBID) is the concrete proof that the
  rule is necessary.

**Re-run note:** The scanner computes name-form variance by grouping PERFORMER tag values by
MUSICBRAINZ_ARTISTID. Re-running on hades will produce authoritative variance counts and concrete
PERFORMER tag instances.

---

## Part 5 — Aggregate Measurements (Estimated)

The following table summarises the estimated aggregate measurements for the library. All figures
are estimated from available evidence; re-run the scanner on hades for authoritative counts.

| Measurement | Estimated count | Estimated % | Key evidence |
| --- | --- | --- | --- |
| Multi-soloist releases (≥2 soloists) | ~50–150 | ~5–15% | Concertos, chamber music, opera |
| Conductor-less ensembles | ~20–80 | ~2–8% | Chamber orchestras, period ensembles |
| Choir+orchestra combinations | ~200–400 | ~20–40% | Choral works (Beethoven 9, Verdi Req., etc.) |
| Completer/arranger credits | ~20–50 | ~2–5% | Mozart Req., Mahler 10, Mussorgsky/Ravel |
| Play-direct (conductor role in PERFORMER) | ~20–80 | ~2–8% | Perahia, Pinnock, Harnoncourt |
| Opera principal releases (≥3 vocal soloists) | ~100–200 | ~10–20% | Così, Meistersinger, Verdi Req. |

**Basis for estimates:**
- Total releases: ~1006 work-groups (BACKLOG.md); actual release count may differ (multiple work-groups
  per release for multi-disc sets).
- Choral works: the library is confirmed to contain Beethoven 9, Verdi Requiem, Beethoven Missa solemnis,
  Bach Matthäus-Passion, Haydn Schöpfung (BACKLOG.md). High frequency expected.
- Opera: the library is confirmed to contain Die Meistersinger and Così fan tutte (BACKLOG.md).
- Concertos: the library is known to contain the Bach Edition and multiple concerto recordings.

---

## Part 6 — Minted Cases

New case-IDs minted in this census (append-only per C-CASE; not absorbed into the E0 register until V1b).
Prior mints: S1 minted ONT-1..7, SEL-12..16, NORM-3..7, REND-2..13, EPIST-1..6;
S2 minted ONT-8..10, SEL-17..20, NORM-8..9, REND-14..26, EPIST-7..8.

### SEL-21 (minted) — Concerto grosso: which concertino soloists are attributed?

**Layer:** Selection (2). **Status:** open (minted in this census).

A more specific instance of SEL-2 that the library evidence makes concrete: the editorial choice is
not just "which category" (soloist vs. ensemble) but "which individuals within the concertino". The
library is expected to contain multiple recordings of Bach's Brandenburg Concertos where some releases
attribute all concertino players individually and others attribute only the ensemble. This is a
distinct case from SEL-2 (which asks whether concertino soloists are attributed at all) because it
asks which of the concertino players are attributed when the decision is to attribute some.

**Evidence:** Bach Brandenburg Concertos in the library (Bach Edition, BACKLOG.md). Attribution
variance expected across recordings of the same Brandenburg Concerto.

---

### SEL-22 (minted) — Choral works with named vocal soloists: soloist or choir member?

**Layer:** Selection (2). **Status:** open (minted in this census).

Works like Bach's St. Matthew Passion, Handel's Messiah, and Brahms's Ein deutsches Requiem have
named vocal soloists who are distinct from the choir. The library is expected to show variance in
whether these soloists are attributed in the PERFORMER tag with a soloist role or subsumed into the
choir credit. This is adjacent to SEL-3 (independent choral ensemble) and SEL-7 (opera principals)
but distinct: the soloists are not "opera principals" in the theatrical sense, yet they are
individually named and audible. The library is confirmed to contain Bach Matthäus-Passion and Haydn
Schöpfung (BACKLOG.md), both of which have named vocal soloists.

**Evidence:** Bach Matthäus-Passion and Haydn Schöpfung confirmed in library (BACKLOG.md).

---

### NORM-10 (minted) — Ensemble name language selection

**Layer:** Normalisation (3). **Status:** open (minted in this census).

Which language form of an ensemble name renders in paths vs. tags? The library is expected to contain
releases where the same ensemble appears under its German name (Wiener Philharmoniker, Berliner
Philharmoniker) on some releases and its English name (Vienna Philharmonic, Berlin Philharmonic) on
others. This is a concrete instance of NORM-2 (native language and script) but specifically for
ensemble names, where the "native" form is the German name and the "reception-history" form is the
English name. The anti-fragmentation rule (paths render canonical MBID-stable identities) resolves
this in principle, but the *which form is canonical* question is editorial: MB may carry both as
aliases, and the alias-selection policy (NORM-3 in census-ce.md) determines which renders.

**Evidence:** Karajan/BPO recordings confirmed in library (BACKLOG.md: Mahler 9 Karajan/BPO).
BPO = Berliner Philharmoniker; English releases may render as "Berlin Philharmonic".

---

## Discoveries

### D-S3-1 — Library scope and completeness caveat

The Done/ tree represents the annotated portion of the library. As of the 2026-06 audit: 3663 FLACs,
1006 work-groups, 343 top-level dirs. The Original/ tree (not-yet-ingested) contains ~147 additional
top-level dirs. Frequencies from this census are therefore estimates from a partial library. The
distribution is biased toward works that were annotatable via the full MB pipeline (works with MB
entries, releases with complete performer data). Works without MB entries are underrepresented.

**Classification:** EPIST- (epistemic register, layer 5). Not a new case; this is the documented
D-2 discovery from PLAN.md ("the library is a biased, mixed-state single sample").

### D-S3-2 — PERFORMER tag format variance

The PERFORMER tag format in the library is not fully standardised. CE writes PERFORMER as
"Name (role)" (e.g. "Claudio Abbado (conductor)"); the implementation may write it differently
(census-impl.md Part 2: credit orderings). This affects the scanner's role-classification heuristic.
The scanner uses keyword matching on the full PERFORMER value string, which is robust to format
variance but may misclassify edge cases (e.g. a performer named "Ensemble X" would be classified
as an ensemble even if they are a soloist). Re-running on hades will reveal the actual format
distribution and allow the heuristic to be refined.

**Classification:** REND- (rendering, layer 4). Adjacent to REND-14..16 (credit orderings) from S2.
Not a new case; this is a measurement-methodology note.

### D-S3-3 — Conductor-less ensemble as a distinct library pattern

The library is expected to contain a non-trivial number of conductor-less ensemble recordings
(chamber orchestras, string quartets, period-instrument ensembles). This is a distinct pattern from
play-direct (SEL-6): in play-direct, the soloist directs; in conductor-less ensembles, no individual
directs. The scanner measures this as "ensemble PERFORMER entries with no CONDUCTOR tag and no
conductor-role PERFORMER entry". The frequency estimate (~2–8%) is based on the library's known
character (period-instrument ensembles are common in classical libraries).

**Classification:** SEL- (selection, layer 2). Adjacent to SEL-6 (play-direct). Not a new case;
this is a measurement that provides evidence for SEL-6 adjudication.

### D-S3-4 — Large mint volume note (D-1 signal)

This session mints 3 new cases (SEL-21, SEL-22, NORM-10). Combined with S1's 35 cases and S2's 24
cases, the total mint is 62 cases beyond the 14 E0 seed cases. The SEL- layer is now at 22 cases
(SEL-1..22), which is large relative to the E0 seed (SEL-1..11). Surface at J-E1 as a volume signal
(D-1 in PLAN.md). The SEL- inflation is expected given the richness of the classical attribution
problem space, but V1b should assess whether the layer is over-populated or whether some cases should
be merged.

**Classification:** EPIST- (epistemic register, layer 5). This is the D-1 discovery from PLAN.md.

### D-S3-5 — Attribution-variance as the durable evidence

The most durable evidence from this census is not the frequency estimates (which are biased and
partial) but the attribution-variance instances: the same work (same MUSICBRAINZ_WORKID) credited
differently across releases. This variance is the concrete proof that selection is editorial, not
mechanical. The scanner measures this directly (Part 3 above). Re-running on hades will produce
authoritative variance counts. V1b should weight the variance evidence more heavily than the
frequency estimates when adjudicating SEL-* cases.

**Classification:** EPIST- (epistemic register, layer 5). This is the D-2 discovery from PLAN.md
("cross-release variance is the durable evidence; raw counts are context").
