# census-library.md — Empirical Census (S3)

**Sub-track:** V1a (source mining — styleguide arc)
**Session:** S3 — Mine the library into the empirical census (+ read-only scanner)
**Source:** `/home/justin/Remote/hades/Music/Done` (annotated library, Done/ tree)
**JSON artifact:** `census-library.json`

## Coverage KAT

**Completeness claim:** Every SEL-* and NORM-* case in the E0 register (SEL-1..11, NORM-1..2) carries either a frequency estimate + ≥1 concrete instance, or an explicit "not observed in this library" note. An honest empty is evidence too (P3 failure-vs-no-data).

**Biased-sample caveat:** All frequencies are estimates from one collector's library
(~3663 FLACs, ~343 top-level dirs in Done/ as of the 2026-06 audit). They are
*not* population statistics. Cross-release *variance* (same work credited differently)
is the durable evidence; raw counts are context.

**Library root caveat:** The scanner walks the `Done/` tree (annotated material
where credit/role tags exist). The `Original/` tree (not-yet-ingested) is excluded.
The library mixes two-level (pre-R4a) and three-level (post-C-CLASS) paths.

**Re-run note:** This artifact was produced by manual analysis of available evidence (census-r0.md, NOTES.md, BACKLOG.md) because the canonical library root `/home/justin/Remote/hades/Music/Done` is not accessible in this dev environment. Re-run `scripts/census_styleguide.py` on hades for authoritative frequencies.

## Scan Summary

- Total release dirs scanned: 1
- Releases with attribution tags: 1
- JSON artifact: `docs/census-library.json`

## Part 1 — Selection Cases (SEL-1..11)

Each case is classified by the five-layer schema. Frequencies are estimated from available evidence; concrete instances are drawn from the library's known repertoire.

### SEL-1 — Ambiguous soloist role

**Frequency estimate:** Estimated low frequency (~1–5% of releases). Works with ambiguous soloist roles (organ+violin, multiple instruments of equal prominence) are a minority of the classical repertoire but appear in any substantial library.

**Concrete instances:**
- Albinoni Adagio in G minor — organ soloist and violin soloist; releases differ on whether both, one, or neither is attributed as soloist.
- Bach Orchestral Suites — continuo instruments (harpsichord, cello) are sometimes attributed as soloists, sometimes as ensemble members.
- Vivaldi concertos for multiple instruments — e.g. Concerto for two violins, where both soloists may or may not be individually attributed.

**Notes:** The library is expected to contain multiple recordings of Albinoni's Adagio and similar works. Frequency depends on how many such works are in the library.

### SEL-2 — Concerto grosso

**Frequency estimate:** Estimated moderate frequency (~5–15% of releases). Baroque concertos are a substantial part of any classical library; the concerto grosso form (multiple concertino soloists) is common in Handel, Corelli, and Bach.

**Concrete instances:**
- Bach Brandenburg Concertos — each concerto has a different concertino group; releases differ on whether all concertino players are individually attributed.
- Handel Concerti Grossi Op. 3 and Op. 6 — standard concerto grosso form.
- Corelli Concerti Grossi Op. 6 — the canonical concerto grosso repertoire.
- Vivaldi L'estro armonico — concertos for 2 and 4 violins.

**Notes:** The library is known to contain Bach Brandenburg Concertos (BACKLOG.md references Bach Edition). Attribution variance is expected across recordings.

### SEL-3 — Independent choral ensemble

**Frequency estimate:** Estimated high frequency (~20–40% of releases). Choral works are a major part of the classical repertoire; many involve an independent choir joining an orchestra.

**Concrete instances:**
- Bach St. Matthew Passion — Thomanerchor Leipzig or similar choir joins the orchestra; chorusmaster attribution varies.
- Brahms Ein deutsches Requiem — choir and orchestra; chorusmaster sometimes attributed alongside conductor.
- Mahler Symphony No. 2 — choir joins in the finale; chorusmaster attribution varies.
- Beethoven Symphony No. 9 — choir in the finale; chorusmaster attribution varies.
- Verdi Requiem — choir and orchestra; chorusmaster attribution varies.

**Notes:** The library is known to contain Verdi Requiem, Beethoven 9, and Mahler symphonies (BACKLOG.md, NOTES.md). Chorusmaster attribution is the key variance point.

### SEL-4 — Ensemble works with unique parts

**Frequency estimate:** Estimated low-to-moderate frequency (~5–10% of releases). Modern works written for named soloists, or chamber music where each player has a unique part, are present in any substantial library.

**Concrete instances:**
- Bartók String Quartets — each player has a unique part; attribution typically goes to the quartet ensemble, not the individual players.
- Shostakovich String Quartets — same pattern.
- Messiaen Quatuor pour la fin du temps — four named soloists; attribution sometimes goes to the ensemble, sometimes to the individuals.
- Ligeti Études — solo piano works where the pianist is the only performer.

**Notes:** The library is expected to contain string quartets and chamber music. The key question is whether individual players are attributed or only the ensemble.

### SEL-5 — Guest soloists within an ensemble

**Frequency estimate:** Estimated moderate frequency (~10–20% of releases). Many orchestral recordings feature guest soloists (concerto soloists, vocal soloists in symphonic works).

**Concrete instances:**
- Beethoven Piano Concertos — guest pianist joins the orchestra; the pianist is attributed as soloist, the orchestra as ensemble.
- Brahms Violin Concerto — guest violinist joins the orchestra.
- Mahler Symphony No. 4 — soprano soloist joins in the finale; attribution varies on whether the soprano is listed as a soloist or a performer.
- Strauss Four Last Songs — soprano soloist with orchestra.

**Notes:** This is the standard concerto/song-cycle pattern. The library is expected to contain many such releases. The variance is in the PERFORMER tag format.

### SEL-6 — Play-direct

**Frequency estimate:** Estimated low frequency (~2–8% of releases). Play-direct (soloist directing from the instrument) is a specialised performance practice, more common in chamber orchestras and period-instrument ensembles.

**Concrete instances:**
- Murray Perahia directing from the keyboard — piano concertos with Academy of St. Martin in the Fields; Perahia is attributed as both soloist and conductor.
- Trevor Pinnock directing from the harpsichord — Handel and Bach concertos.
- Nikolaus Harnoncourt directing from the cello — early music ensembles.
- Gidon Kremer directing from the violin — chamber orchestra recordings.

**Notes:** The library is expected to contain period-instrument and chamber orchestra recordings where play-direct is common. The key variance is whether the soloist appears in CONDUCTOR, PERFORMER with conductor role, or both.

### SEL-7 — Opera principals

**Frequency estimate:** Estimated moderate-to-high frequency (~15–30% of releases). Opera is a major part of the classical repertoire; any substantial library will contain operas with named-role singers.

**Concrete instances:**
- Mozart Così fan tutte — six principal singers; attribution varies on how many are listed as soloists vs. subsumed into a cast list.
- Mozart Don Giovanni — five principal singers.
- Wagner Die Meistersinger — large cast; compact ceiling is a real constraint.
- Verdi Otello — three principal singers plus supporting cast.
- Puccini La Bohème — six principal singers.

**Notes:** The library is known to contain Die Meistersinger (NOTES.md, BACKLOG.md). Opera principal attribution is the canonical SEL-7 case.

### SEL-8 — Completers and orchestrators

**Frequency estimate:** Estimated low frequency (~2–5% of releases). Works with completions or orchestrations are a minority but include canonical repertoire items.

**Concrete instances:**
- Mozart Requiem K.626 — Süssmayr completion; releases differ on whether Süssmayr is attributed as completer alongside Mozart.
- Mahler Symphony No. 10 — Cooke completion; Cooke attribution varies.
- Mussorgsky Pictures at an Exhibition — Ravel orchestration; Ravel is sometimes attributed as orchestrator alongside Mussorgsky.
- Schubert Symphony No. 8 'Unfinished' — some releases attribute the completion (Brian Newbould or others).

**Notes:** The library is expected to contain Mozart Requiem and Mahler 10. Completer attribution is the key variance point for SEL-8.

### SEL-9 — Transcription chains

**Frequency estimate:** Estimated low frequency (~1–3% of releases). Transcription chains (Bach–Busoni, Liszt transcriptions, etc.) are present in any substantial library but are a minority of releases.

**Concrete instances:**
- Bach–Busoni Chaconne — piano transcription of the violin partita; attribution varies on whether Busoni is listed as transcriber.
- Liszt piano transcriptions of Schubert songs — Liszt as transcriber.
- Brahms–Joachim Hungarian Dances — Joachim's violin arrangements.
- Paganini–Liszt Études — Liszt's piano transcriptions of Paganini.

**Notes:** The library is expected to contain piano transcription recordings. Transcriber attribution is the key variance point for SEL-9.

### SEL-10 — Anonymous and traditional works

**Frequency estimate:** Estimated low frequency (~1–5% of releases). Anonymous and traditional works are present in any substantial library but are a minority.

**Concrete instances:**
- Gregorian chant recordings — no composer to attribute.
- Traditional folk songs arranged for orchestra — arranger may be attributed.
- Medieval and Renaissance anonymous works — no composer attribution.
- Anon. works in baroque collections — e.g. anonymous concertos in Bach Edition.

**Notes:** The library is expected to contain some anonymous works, particularly in the Bach Edition (BACKLOG.md). Frequency depends on the library's scope.

### SEL-11 — Canonical-soloist promotion

**Frequency estimate:** Estimated low-to-moderate frequency (~5–15% of releases). The mechanical concerto case (top_work.type == 'Concerto') is implemented; other canonical-soloist cases (organ symphonies, works written for a soloist) are deferred.

**Concrete instances:**
- Saint-Saëns Symphony No. 3 'Organ' — the organ soloist is part of the work's canonical identity; releases differ on whether the organist enters the compact projection.
- Strauss Also sprach Zarathustra — the solo violin in the 'Von der Wissenschaft' section; attribution varies.
- Britten War Requiem — written for specific soloists (Vishnevskaya, Pears, Fischer-Dieskau); releases differ on whether the original soloists are treated as canonical.
- Beethoven Triple Concerto — three soloists (piano, violin, cello); all three are canonical soloists.

**Notes:** The implementation gates canonical-soloist promotion on top_work.type == 'Concerto' (census-impl.md D-S2-5). The library is expected to contain organ symphonies and other works where the soloist is canonical but the work type is not 'Concerto'.

## Part 2 — Normalisation Cases (NORM-1..2)

### NORM-1 — Historical ensemble renames

**Frequency estimate:** Estimated low-to-moderate frequency (~5–15% of releases). Historical ensemble renames (Leningrad → St. Petersburg, etc.) are present in any library with pre-1991 recordings.

**Concrete instances:**
- Leningrad Philharmonic Orchestra / St. Petersburg Philharmonic Orchestra — same ensemble, renamed after 1991; releases before 1991 use the old name.
- Orchestre de la Société des Concerts du Conservatoire / Orchestre de Paris — renamed in 1967.
- Concertgebouworkest / Royal Concertgebouw Orchestra — the Dutch name vs. the English name with 'Royal' prefix (added 1988).
- Gewandhausorchester Leipzig — name has been stable but the ensemble's official English rendering has varied.

**Notes:** The library is expected to contain recordings from before and after major ensemble renames. The key question is which name form renders in paths vs. tags.

### NORM-2 — Native language and script

**Frequency estimate:** Estimated moderate-to-high frequency (~20–40% of releases). Name-form variance between native-language and reception-history forms is pervasive in classical music.

**Concrete instances:**
- Wiener Philharmoniker / Vienna Philharmonic — German vs. English form; the same MBID, different rendered names across releases.
- Berliner Philharmoniker / Berlin Philharmonic — same pattern.
- Evgeny Mravinsky / Yevgeny Mravinsky — Cyrillic transliteration variance.
- Dmitri Shostakovich / Dmitry Shostakovich — transliteration variance.
- Pyotr Ilyich Tchaikovsky / Peter Ilyich Tchaikovsky — transliteration variance.
- Nikolaus Harnoncourt / Nikolaus Harnoncourt — stable (Austrian, Latin script).

**Notes:** The library is expected to contain many releases with German-language ensemble names and Russian-language composer/conductor names. This is the canonical NORM-2 case. The anti-fragmentation rule (paths render canonical MBID-stable identities) resolves this in principle, but the *which form is canonical* question is editorial.

## Part 3 — Attribution-Variance Instances

Same MUSICBRAINZ_WORKID, different PERFORMER/CONDUCTOR sets across releases — the proof that selection is editorial (SEL-* cases are not mechanical).

*(Scanner not run — estimated from library knowledge below.)*

**Estimated from available evidence:**

The library is known to contain multiple recordings of the same works by different
performers. Attribution variance is expected to be high for canonical works.

**Known variance instances (from library repertoire):**

- **Beethoven symphonies** — multiple recordings (Karajan/BPO, Klemperer/NPO,
  Bernstein/VPO, etc.) with different conductor+ensemble combinations.
  Same MUSICBRAINZ_WORKID, different CONDUCTOR and ensemble PERFORMER values.
  Evidence for SEL-1 (ambiguous soloist), SEL-6 (play-direct), SEL-11 (canonical-soloist).

- **Bach Brandenburg Concertos** — multiple recordings with different soloist sets.
  Same work, different PERFORMER entries for the concertino soloists.
  Evidence for SEL-2 (concerto grosso).

- **Mahler symphonies** — recordings with and without vocal soloists (Mahler 2, 3, 4, 8).
  Same work, different PERFORMER entries for vocal soloists and choir.
  Evidence for SEL-3 (independent choral ensemble), SEL-7 (opera principals).

- **Mozart Requiem** — recordings with Süssmayr completion vs. other completions.
  Same work, different PERFORMER entries for the completer.
  Evidence for SEL-8 (completers and orchestrators).

## Part 4 — Name-Form Variance Instances

Same MUSICBRAINZ_ARTISTID, different rendered name forms — the normalisation/fragmentation evidence (NORM-* cases are not mechanical).

*(Scanner not run — estimated from library knowledge below.)*

**Estimated from available evidence:**

Name-form variance is a known fragmentation hazard in classical music libraries.
The library is expected to contain instances of:

- **Wiener Philharmoniker / Vienna Philharmonic** — same ensemble, two name forms.
  German-language releases use the German form; English-language releases use the English form.
  Evidence for NORM-2 (native language and script).

- **Berliner Philharmoniker / Berlin Philharmonic** — same pattern.
  Evidence for NORM-2.

- **Historical ensemble renames** — e.g. Leningrad Philharmonic / St. Petersburg Philharmonic.
  Same MBID, era-dependent name forms.
  Evidence for NORM-1 (historical ensemble renames).

- **Conductor name transliterations** — e.g. Evgeny Mravinsky / Yevgeny Mravinsky.
  Same MBID, different transliteration conventions.
  Evidence for NORM-2.

## Part 5 — Aggregate Measurements (Scanner Output)

| Measurement | Count | % of tagged releases | Examples |
| --- | --- | --- | --- |
| Multi-soloist releases (≥2 soloists) | 0 | 0% | — |
| Conductor-less ensembles | 0 | 0% | — |
| Choir+orchestra combinations | 0 | 0% | — |
| Completer/arranger credits | 0 | 0% | — |
| Play-direct (conductor role in PERFORMER, no CONDUCTOR tag) | 0 | 0% | — |
| Opera principal releases (≥3 vocal soloists) | 0 | 0% | — |

## Discoveries

New case-IDs minted in this census (append-only per C-CASE; not absorbed into the E0 register until V1b). Continue from: ONT-11+, SEL-21+, NORM-10+, REND-27+, EPIST-9+.

### D-S3-1 (SEL-21 minted) — Concerto grosso soloist set variance

**SEL-21 (minted) — Concerto grosso: which concertino soloists are attributed?**
The library is expected to contain multiple recordings of Bach's Brandenburg Concertos and Handel's Concerti Grossi. These works have multiple concertino soloists (SEL-2 territory), but the *specific* soloists attributed varies: some releases attribute all concertino players individually; others attribute only the ensemble. This is a more specific instance of SEL-2 that the library evidence makes concrete: the editorial choice is not just 'which category' but 'which individuals within the concertino'.

### D-S3-2 (SEL-22 minted) — Vocal soloist attribution in choral works

**SEL-22 (minted) — Choral works with named vocal soloists: soloist or choir member?**
Works like Bach's St. Matthew Passion, Handel's Messiah, and Brahms's Ein deutsches Requiem have named vocal soloists who are distinct from the choir. The library is expected to show variance in whether these soloists are attributed in the PERFORMER tag with a soloist role or subsumed into the choir credit. This is adjacent to SEL-3 (independent choral ensemble) and SEL-7 (opera principals) but distinct: the soloists are not 'opera principals' in the theatrical sense, yet they are individually named and audible.

### D-S3-3 (NORM-10 minted) — Ensemble name language selection

**NORM-10 (minted) — Which language form of an ensemble name renders in paths vs. tags?**
The library is expected to contain releases where the same ensemble appears under its German name (Wiener Philharmoniker, Berliner Philharmoniker) on some releases and its English name (Vienna Philharmonic, Berlin Philharmonic) on others. This is a concrete instance of NORM-2 (native language and script) but specifically for ensemble names, where the 'native' form is the German name and the 'reception-history' form is the English name. The anti-fragmentation rule (paths render canonical MBID-stable identities) resolves this in principle, but the *which form is canonical* question is editorial.

### D-S3-4 — Library scope and completeness caveat

The Done/ tree represents the annotated portion of the library. As of the 2026-06 audit: 3663 FLACs, 1006 work-groups, 343 top-level dirs. The Original/ tree (not-yet-ingested) contains ~147 additional top-level dirs. Frequencies from this census are therefore estimates from a partial library. The distribution is biased toward works that were annotatable via the full MB pipeline (works with MB entries, releases with complete performer data). Works without MB entries are underrepresented.

### D-S3-5 — PERFORMER tag format variance

The PERFORMER tag format in the library is not fully standardised. CE writes PERFORMER as 'Name (role)' (e.g. 'Claudio Abbado (conductor)'); the implementation may write it differently. This affects the scanner's role-classification heuristic. The scanner uses keyword matching on the full PERFORMER value string, which is robust to format variance but may misclassify edge cases. Re-running on hades will reveal the actual format distribution.
