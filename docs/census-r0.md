# Census R0 — Final Artifact (Pass 1 + Pass 2 + Adjudication)

Generated from `census-r0.json` — 147 top-level dirs in `Original/`.
Pass 1: offline evidence sweep. Pass 2: MB network lookups. Adjudication: user-resolved residuals.

## Joint Distribution (Axis 1 × Axis 2)

Axis 1 = provenance; Axis 2 = MB status. Counts are final post-adjudication.

| Provenance \ MB Status | already-ingested     | in-mb-clean          | in-mb-mismatch       | not-in-mb            | non-classical-other  | unknown              | **Total** |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **presto** | 0                    | 36                   | 9                    | 0                    | 1                    | 0                    | **46** |
| **whipper** | 1                    | 52                   | 5                    | 5                    | 5                    | 0                    | **68** |
| **amazon** | 0                    | 0                    | 0                    | 0                    | 1                    | 0                    | **1** |
| **other-download** | 0                    | 19                   | 4                    | 0                    | 0                    | 0                    | **23** |
| **unknown** | 0                    | 0                    | 0                    | 1                    | 8                    | 0                    | **9** |
| **Total** | **1** | **107** | **18** | **6** | **15** | **0** | **147** |

## J1 Handoff: Per-Class Populations

R3 adapter order and R2 rung-ladder shape depend on these populations. R4a consumes the non-classical-other inventory.

### R3a — presto / in-mb-clean (36 dirs)

Presto downloads with confirmed MB release. Direct ingest candidates.

- `4833954 - Hilary Hahn plays Bach`
- `ALC 3146 - Leopold Stokowski - Complete Everest  Vanguard Masters`
- `Alkan - Edition`
- `Anne Sofie von Otter sings Offenbach`
- `Autour de la Harpe`
- `Bayer - Die Puppenfee (The Fairy Doll)`
- `Beethoven - Fur Elise and Bagatelles Opp. 33, 119`
- `Beethoven - String Quartets Nos. 1-16 (complete, `
- `Beethoven Piano Concerto No.1, No.2 - Argerich - Sinopoli - Philharmonia Orchestra [4156822]`
- `Beethoven Violin Concerto, Romance No.1, No.2 - Mintz - Sinopoli - Philharmonia Orchestra [E4230642]`
- `Berlioz Odyssey`
- `Berlioz: Requiem (Grande Messe des morts) [Live]`
- `CHAN9177 - Barber Complete Works for Solo Piano`
- `Debussy - Les Trois Sonates, The Late Works`
- `Debussy - Sonatas & Trios`
- `Delibes - Coppelia & La Source Suite`
- `Mendelssohn - A Midsummer Night's Dream - incidental music - Deutsch, Op.61`
- `Mendelssohn - A Midsummer Night's Dream - incidental music - English, Op.61`
- `NSO0023 - Barber Vanessa`
- `ONYX4129 - American Chamber Music`
- `Offenbach Folies symphoniques & Ouvertures`
- `Prokofiev - Symphony No. 5 & Stravinsky - The Rite of Spring`
- `Prokofiev Romeo and Juliet, Op.64`
- `RES10301 - Barber The Complete Songs`
- `Ravel - Introduction & Allegro, etc`
- `Richard Strauss Also sprach Zarathustra, Don Juan, Salome Scene No.4 - Sinopoli - New York Philharmonic - Staatskapelle Dresden - Deutsche Oper Berlin [E4745662]`
- `Saint-Saëns Symphonies - Mǎcelaru - Orchestre National de France.9029653343`
- `Schubert - Quintet in C D956 & Quartettsatz D703`
- `Schubert - Symphonies Nos. 3 & 8`
- `Shostakovich Symphony No.9, No.5, Suite from Hamlet, Symphony No.8 - Nelsons - Boston Symphony [4795201]`
- `Shostakovich Леди Макбет Мценского уезда Passacaglia, Symphony No.10 - Nelsons - Boston Symphony [4795059]`
- `Shostakovich, Stravinsky - Bernstein - Complete Deutsche Grammphon`
- `Sibelius Violin Concerto, Prokofiev Violin Concerto, Sibelius Vesipisaroita - Jansen - Mäkelä - Oslo-Filharmonien [4854748]`
- `Steinberg Passion Week - Fox - Clarion Choir.8573665`
- `Swedish Orchestral Favourites Vol.1.8553115`
- `Swedish Orchestral Favourites Vol.2.8553715`

### R3b — whipper / in-mb-clean (52 dirs)

Whipper rips with confirmed MB release (embedded MBID or Pass 2 match). Direct ingest candidates.

- `Antonio Vivaldi; Amsterdam Baroque Orchestra, Yo‐Yo Ma, Ton Koopman - Vivaldi’s Cello (US Sony Classical (SK 90916) Release)`
- `Arturo Toscanini :: Beethoven (NBC Symphony Orchestra) (Disc 6).0x310f2704`
- `Arturo Toscanini :: Ludwig van Beethoven Symphonies Nos. 1,2,3&4 Vol 1.0x670e3a09`
- `Arturo Toscanini :: NBC Symphony Orchestra Vol II: Ludwig van Beethoven CD2.0x7f0da908`
- `Bach, J.S. :: Symphonic Bach - Orchestral Transcriptions by Respigi and Elgar.0xac0d6e0c`
- `Beethoven  :: Symphonies Nos. 3&4 - Toscanini, NBCSO.0x73121308`
- `Beethoven :: Beethoven (NBC Symphony Orchestra) (Disc 5).0x3d117c05`
- `Beethoven, Ludwig Van:  :: Beethoven: Complete Sym & Con for Pno; Barenboim::PO::NPO::Klemperer.0x5d118508`
- `Chicago Jazz Philharmonic - Collective Creativity.0xb80c360c`
- `Ella Fitzgerald & Louis Armstrong :: The Best Of Ella Fitzgerald & Louis Armstrong.0xea0ff30f`
- `English Class Hits! June 2006.0x37123416`
- `Franz Schubert; Takács Quartet - Death and the Maiden`
- `Ginastera · Boieldieu Harp Concertos - Zoff, Kurz, Staatskapelle Dresden`
- `Gioacchino Rossini :: Panorama - Gioacchino Rossini - Disc 1.0x9b10f20b`
- `Gioacchino Rossini :: Panorama.0xe6120d0f`
- `Igor Stravinsky - Columbia Symphony Orchestra :: Stravinsky Conducts - The Firebird Suite - Petrushka Ballet Suite.0x6c0c8818`
- `Jane Austen Entertains :: Music from Her Own Library.0x100ff515`
- `Joshua Bell - Romance of the Violin`
- `Kathleen Battle, Placido Domingo, Metropolitan Opera Orchestra - James Levine :: Live in Tokyo 1988 - Kathleen Battle, Placido Domingo, Metropolitan Opera Orchestra - James Levine.0x660e0809`
- `Marvin Goldstein :: Inspirational Notes.0x9708d90c`
- `Mascagni, Pietro: :: Mascagni: Karajan the Opera Recordings; Karajan::Orch Del Teatro alla Scala.0x2f12eb16`
- `Merill Jensen :: Come Unto Christ - The Conversion of Alma the Younger.0xc20f550e`
- `Michael Bolton :: Time, Love & Tenderness.0x880a820a`
- `Mormon Tabernacle Choir feat. Santino Fontana and The Muppets from Sesame Street - Keep Christmas With You`
- `Mormon Tabernacle Choir, Orchestra at Temple Square, Mack Wilberg - Praise to the Man`
- `Music Together - Bongos (Family Edition 2023)`
- `Music Together - Tambourine (Family Edition 2022)`
- `ProVocal Volume 12: Ella Fitzgerald.0xf60c8711`
- `Red and Yellow.0x870a410a`
- `Reid Nibley :: Sabbath Song.0x11118d12`
- `Schubert String Quintet - Brandis Quartet, Jorg Baumann.Apex.Teldec.0x2a0cfb04`
- `Schubert String Quintet I, II - Piacevole Quintet.0x39071b05`
- `Schubert; Takács Quartet, Ralph Kirshbaum - String Quintet, D. 956 _ String Quartet, D. 703 “Quartettsatz”`
- `Schumann, Robert: :: Schumann: Con for Pno::Con for Clo; Gieseking::Machula::BPO::Furtwangler.0x570c5306`
- `Strauss, Richard: :: Strauss: Sinfonia Domestica::Don Juan; Furtwangler::BPO.0x4d0e0b06`
- `Stravinsky, Igor (1882-1971) :: Le Sacre du Printemps (Stravinsky).0xd4084d0f`
- `The Choirs and Orchestra of Brigham Young University :: Songs of Praise and Remembrance.0xd10d400f`
- `The Feel Good CD.0xd90a370d`
- `Unknown Artist - aKkt9ShtSOg1Acgk8ealeqP6NqE-`
- `Unknown Artist - qlSQ2jDIvQP22d9x8JFZAo5Om4Y-`
- `Various :: Jazz Classic, 10th ED, CD03.0xb912740c`
- `Various :: Jazz Classics, 10th ED, CD01.0xd712ac1f`
- `Various :: Jazz Classics, 10th ED, CD02.0xa912340c`
- `Various :: Preludes, Fugues and Riffs - Jazz in Classical Music.0xab12490d`
- `Various :: Spirit Of The 60's [Blue}.0x6206170a`
- `Various :: Symphony No. 3- Organ.0x5b0e2b07`
- `Various :: The Irving Berlin 100th Anniversary Collection`
- `Wagner, Richard: :: Wagner: Ring Des Nibelungen; Karajan::BPO.0x6b0a9b08`
- `Wagner, Richard: :: Wagner: Ring Des Nibelungen; Karajan::BPO.0xaf11030d`
- `Wilhelm Furtwängler :: [Disc 2] Johannes Brahms.0x1a0b2104`
- `Wilhelm Furtwängler :: [Disc 3] Anton Bruckner.0x2f0fe904`
- `Wilhelm Furtwängler :: [Disk 5] Sibelius, Strauss, Ravel.0x180c3b03`

### R3c — not-in-mb (6 dirs)

No MB release found. Feeds Discogs/manual-entry adjudication at J1.

- `Dave Eaton Show Live.0xc50f780d` (axis1=whipper)
  - Personal live show recording; auto-classified as in-mb-clean via dubious MB match (Steve Eaton, score 100 but different 
- `Durham Mus Theater.0x43113c15` (axis1=whipper)
  - Theater music rip; auto-classified as in-mb-clean via dubious MB match (Richard & Linda Thompson live, score 78); adjudi
- `Karen #4.0xd00f0610` (axis1=whipper)
  - Personal recording (Karen #4); no plausible MB match; adjudicated 2026-07-20
- `Karen Song #1.0x6f07de09` (axis1=whipper)
  - Personal recording (Karen Song #1); auto-classified as in-mb-clean via dubious MB match; adjudicated 2026-07-20
- `LDS Youth Music` (axis1=unknown)
  - User: personal/religious LDS youth music collection, not in MB as a unit; adjudicated 2026-07-20; User: will manually mo
- `Ric's Class Music.0x9812024d` (axis1=whipper)
  - Personal class music collection (77 tracks); no plausible MB match; adjudicated 2026-07-20

### R3d — in-mb-mismatch (18 dirs)

MB release found but track counts / edition disagree. Needs MB edit or manual reconciliation.

- `Daniel Barenboim & Orchestre de Paris` (axis1=other-download)
  - MB candidates found but track counts disagree: best='Daniel Barenboim Dirigiert' score=100, tracks=4
- `G010001967730Z - The Music Of America - Samuel Barber` (axis1=presto)
  - MB candidates found but track counts disagree: best='Music of Samuel Barber' score=100, tracks=6 vs 
- `Glazunov Complete Symphonies.19642a` (axis1=other-download)
  - MB candidates found but track counts disagree: best='Glazunov Piano Transcriptions' score=100, track
- `Grieg Edition` (axis1=presto)
  - MB candidates found but track counts disagree: best='Grieg Edition' score=100, tracks=507 vs local=3
- `Herbert von Karajan - Invitation to the Dance` (axis1=presto)
  - MB candidates found but track counts disagree: best='Herbert von Karajan' score=100, tracks=73 vs lo
- `Karajan Sampler` (axis1=presto)
  - MB candidates found but track counts disagree: best='Karajan' score=100, tracks=40 vs local=82
- `Lifescapes :: Celtic Christmas.0xcf0c250e` (axis1=whipper)
  - MB candidates found but track counts disagree: best='Lifescapes: Celtic Christmas' score=100, tracks
- `Mussorgsky - Pictures at an Exhibition` (axis1=presto)
  - MB candidates found but track counts disagree: best='Mussorgsky - Pictures at an Exhibition' score=1
- `Mussorgsky: Romances and Songs` (axis1=other-download)
  - MB candidates found but track counts disagree: best='Songs and Romances' score=100, tracks=16 vs loc
- `Prokofiev Symphony No.5, Stravinsky The Rite of Spring - Karajan - Berliner Philharmoniker` (axis1=presto)
  - MB candidates found but track counts disagree: best='Karajan: Berliner Philharmoniker' score=100, tr
- `Puccini Madama Butterfly - Freni - Carreras - Pons - Berganza - Sinopoli - Philharmonia Orchestra [4779128]` (axis1=presto)
  - MB candidates found but track counts disagree: best='– – –' score=100, tracks=3 vs local=34
- `Shostakovich Symphony No.1-15, October, Над Родиной нашей солнце сияет, Казнь Степана Разина, Violin Concerto No.2 [RCID18056928]` (axis1=other-download)
  - MB candidates found but track counts disagree: best='Shostakovich: Violin Concerto no. 1 / Prokofiev
- `Shostakovich Symphony No.4, No.11 "1905-й год" - Nelsons - Boston Symphony [4835220]` (axis1=presto)
  - MB candidates found but track counts disagree: best='Stokowski Shostakovich Symphony No. 11 ("1905")
- `Tchaikovsky Complete Symphonies - Jansons - Oslo-Filharmonien.CHAN10392(6)` (axis1=presto)
  - MB candidates found but track counts disagree: best='Symphonies - Complete' score=100, tracks=62 vs 
- `Various :: Spirit Of The 60's [Green].0x6705a60a` (axis1=whipper)
  - MB candidates found but track counts disagree: best='Spirit of the 60's' score=100, tracks=34 vs loc
- `Wagner, Richard: :: Wagner: Das Rheingold.0x56123b18` (axis1=whipper)
  - MB candidates found but track counts disagree: best='Wagner : Das Rheingold' score=100, tracks=44 vs
- `Wagner, Richard: :: Wagner: Das Rheingold; Karajan::BPO.0x450ff514` (axis1=whipper)
  - MB candidates found but track counts disagree: best='Wagner : Das Rheingold' score=100, tracks=44 vs
- `Wolfgang Amadeus Mozart :: Complete Mozart Edition, Vol. 6: Dances & Marches, Disc 1.0xdd0f3323` (axis1=whipper)
  - MB candidates found but track counts disagree: best='Wolfgang Amadeus Mozart' score=100, tracks=25 v

### R3e — other-download or amazon / in-mb-clean (19 dirs)

Non-Presto downloads with confirmed MB release. Ingest candidates.

- `4757765 - Berlioz Requiem` (axis1=other-download)
- `4870712 - Gould Spirituals; Fall River Legend; Barber Medea` (axis1=other-download)
- `6872862 - Samuel Barber Adagio` (axis1=other-download)
- `9029661011 - Itzhak Perlman Plays Tchaikovsky` (axis1=other-download)
- `American Composers Choral Festibal: An Evening with Mack J. Wilberg` (axis1=other-download)
- `Elgar Cello Concerto, Enigma Variations, Pomp and Circumstance March No.1, No.4 - Maisky - Sinopoli - Philharmonia Orchestra [4783619]` (axis1=other-download)
- `Françaix - L’Horloge de Flore.999779-2` (axis1=other-download)
- `Glinka & Glazunov - Chamber Music` (axis1=other-download)
- `Grieg Complete Orchestral Works` (axis1=other-download)
- `Handel The Messiah - Wilberg - Orchestra and Choir at Temple Square` (axis1=other-download)
- `Humperdinck - Hansel und Gretel` (axis1=other-download)
- `Mussorgsky Sorochintsy Fair` (axis1=other-download)
- `Prokofiev Peter and the Wolf, Op. 67` (axis1=other-download)
- `RCID15847740 - Tchaikovsky Complete Romances` (axis1=other-download)
- `Saint-Saens: Carnival of the Animals, Organ Symphony & other orchestral works` (axis1=other-download)
- `Saint-Saëns Symphonies - Martinon - Orchestre National de l’ORTF.6318042` (axis1=other-download)
- `Shostakovich Selected Symphonies, Песнь о лесах - Mravinsky - Санкт-Петербургская филармония` (axis1=other-download)
- `Shostakovich Symphony No.4, No.5, No.6 - Mäkelä - Oslo-Filharmonien [4854637]` (axis1=other-download)
- `Stravinsky - Orchestral Works` (axis1=other-download)

## Already-Ingested Delete-Candidates (Evidence Detail)

These dirs have journal matches with destinations present under `Done/`.
Evidence level: journal-entry count / destination-present count / source-file count.
Deletion is R5 operator work — do not delete until R5 drain.

- `Haydn String Quartets - The Angeles Quartet - disc 15.0x920e900c` — journal: 12, dest present: 12, source files: 12

## Non-Classical-Other Inventory for R4a (15 dirs)

These dirs are outside the classical corpus. R4a must admit them in the Act II taxonomy.

- `Aesop_Fables.0xe60a3e11` (axis1=whipper)
  - Audiobook/spoken word (Aesop's Fables); auto-classified as in-mb-clean via MB match; adjudicated 2026-07-20
- `Amazon Music` (axis1=amazon genres=['R&B', 'Pop', 'International', 'Dance & DJ', 'Miscellaneous', 'Jazz', 'New Age', "Children's Music", 'Broadway & Vocalists', 'Soundtracks', 'Classical', 'Rap & Hip-Hop', 'Christian & Gospel'])
- `Audiobooks` (axis1=unknown genres=['Biography & Autobiography'])
  - User: will manually move out of library; axis1=unknown (no provenance signal); adjudicated 2026-07-20
- `Caro mio ben` (axis1=unknown)
  - User: will move out of library; single MP3 of classical aria, not a full release; adjudicated 2026-07-20; User: will man
- `Disc Cleaner.0x720f9e07` (axis1=whipper)
  - CD cleaning disc, not music; auto-classified as in-mb-clean via dubious MB match (Head Cleaner by Popof); adjudicated 20
- `Education` (axis1=whipper genres=['Pop'])
  - Pop music with Amazon tags; auto-classified as in-mb-clean via dubious MB match; adjudicated 2026-07-20
- `GarageBand` (axis1=unknown)
  - User: will manually move out of library; axis1=unknown (GarageBand project files); adjudicated 2026-07-20
- `HypnoBirthing Tracks` (axis1=presto genres=['New Age'])
- `Into The Woods Piano Accompaniment` (axis1=unknown)
  - User: will move out; piano accompaniment tracks for musical theater rehearsal, not classical; adjudicated 2026-07-20; Us
- `Kidz Bop Kids :: Kidz Bop 33.0x990a330e` (axis1=whipper)
- `Lydia Ballet Exercises` (axis1=unknown)
  - User: will manually move out of library; axis1=unknown (no provenance signal); adjudicated 2026-07-20
- `Lydia Dance Repertoire Music` (axis1=unknown genres=['Soundtrack'])
  - User: will move out; personal dance competition music (YAGP submission files), not classical; adjudicated 2026-07-20; Us
- `Playlists` (axis1=unknown)
  - User: playlist files only, no audio content; adjudicated 2026-07-20; User: playlist files to be transferred to new playl
- `Various Artists :: Kidz Bop 37.0xd60a640f` (axis1=whipper)
- `nachtmusick` (axis1=unknown genres=['Classical'])
  - User: will move out; personal classical MP3 collection folder (79 tracks incl. Messiah subdir), not a single release; ad

## Full Per-Class Directory Listings

### presto / in-mb-clean (36 dirs)

- `4833954 - Hilary Hahn plays Bach`
- `ALC 3146 - Leopold Stokowski - Complete Everest  Vanguard Masters`
- `Alkan - Edition`
- `Anne Sofie von Otter sings Offenbach`
- `Autour de la Harpe`
- `Bayer - Die Puppenfee (The Fairy Doll)`
- `Beethoven - Fur Elise and Bagatelles Opp. 33, 119`
- `Beethoven - String Quartets Nos. 1-16 (complete, `
- `Beethoven Piano Concerto No.1, No.2 - Argerich - Sinopoli - Philharmonia Orchestra [4156822]`
- `Beethoven Violin Concerto, Romance No.1, No.2 - Mintz - Sinopoli - Philharmonia Orchestra [E4230642]`
- `Berlioz Odyssey`
- `Berlioz: Requiem (Grande Messe des morts) [Live]`
- `CHAN9177 - Barber Complete Works for Solo Piano`
- `Debussy - Les Trois Sonates, The Late Works`
- `Debussy - Sonatas & Trios`
- `Delibes - Coppelia & La Source Suite`
- `Mendelssohn - A Midsummer Night's Dream - incidental music - Deutsch, Op.61`
- `Mendelssohn - A Midsummer Night's Dream - incidental music - English, Op.61`
- `NSO0023 - Barber Vanessa`
- `ONYX4129 - American Chamber Music`
- `Offenbach Folies symphoniques & Ouvertures`
- `Prokofiev - Symphony No. 5 & Stravinsky - The Rite of Spring`
- `Prokofiev Romeo and Juliet, Op.64`
- `RES10301 - Barber The Complete Songs`
- `Ravel - Introduction & Allegro, etc`
- `Richard Strauss Also sprach Zarathustra, Don Juan, Salome Scene No.4 - Sinopoli - New York Philharmonic - Staatskapelle Dresden - Deutsche Oper Berlin [E4745662]`
- `Saint-Saëns Symphonies - Mǎcelaru - Orchestre National de France.9029653343`
- `Schubert - Quintet in C D956 & Quartettsatz D703`
- `Schubert - Symphonies Nos. 3 & 8`
- `Shostakovich Symphony No.9, No.5, Suite from Hamlet, Symphony No.8 - Nelsons - Boston Symphony [4795201]`
- `Shostakovich Леди Макбет Мценского уезда Passacaglia, Symphony No.10 - Nelsons - Boston Symphony [4795059]`
- `Shostakovich, Stravinsky - Bernstein - Complete Deutsche Grammphon`
- `Sibelius Violin Concerto, Prokofiev Violin Concerto, Sibelius Vesipisaroita - Jansen - Mäkelä - Oslo-Filharmonien [4854748]`
- `Steinberg Passion Week - Fox - Clarion Choir.8573665`
- `Swedish Orchestral Favourites Vol.1.8553115`
- `Swedish Orchestral Favourites Vol.2.8553715`

### presto / in-mb-mismatch (9 dirs)

- `G010001967730Z - The Music Of America - Samuel Barber`
- `Grieg Edition`
- `Herbert von Karajan - Invitation to the Dance`
- `Karajan Sampler`
- `Mussorgsky - Pictures at an Exhibition`
- `Prokofiev Symphony No.5, Stravinsky The Rite of Spring - Karajan - Berliner Philharmoniker`
- `Puccini Madama Butterfly - Freni - Carreras - Pons - Berganza - Sinopoli - Philharmonia Orchestra [4779128]`
- `Shostakovich Symphony No.4, No.11 "1905-й год" - Nelsons - Boston Symphony [4835220]`
- `Tchaikovsky Complete Symphonies - Jansons - Oslo-Filharmonien.CHAN10392(6)`

### presto / non-classical-other (1 dirs)

- `HypnoBirthing Tracks`

### whipper / already-ingested (1 dirs)

- `Haydn String Quartets - The Angeles Quartet - disc 15.0x920e900c`

### whipper / in-mb-clean (52 dirs)

- `Antonio Vivaldi; Amsterdam Baroque Orchestra, Yo‐Yo Ma, Ton Koopman - Vivaldi’s Cello (US Sony Classical (SK 90916) Release)`
- `Arturo Toscanini :: Beethoven (NBC Symphony Orchestra) (Disc 6).0x310f2704`
- `Arturo Toscanini :: Ludwig van Beethoven Symphonies Nos. 1,2,3&4 Vol 1.0x670e3a09`
- `Arturo Toscanini :: NBC Symphony Orchestra Vol II: Ludwig van Beethoven CD2.0x7f0da908`
- `Bach, J.S. :: Symphonic Bach - Orchestral Transcriptions by Respigi and Elgar.0xac0d6e0c`
- `Beethoven  :: Symphonies Nos. 3&4 - Toscanini, NBCSO.0x73121308`
- `Beethoven :: Beethoven (NBC Symphony Orchestra) (Disc 5).0x3d117c05`
- `Beethoven, Ludwig Van:  :: Beethoven: Complete Sym & Con for Pno; Barenboim::PO::NPO::Klemperer.0x5d118508`
- `Chicago Jazz Philharmonic - Collective Creativity.0xb80c360c`
- `Ella Fitzgerald & Louis Armstrong :: The Best Of Ella Fitzgerald & Louis Armstrong.0xea0ff30f`
- `English Class Hits! June 2006.0x37123416`
- `Franz Schubert; Takács Quartet - Death and the Maiden`
- `Ginastera · Boieldieu Harp Concertos - Zoff, Kurz, Staatskapelle Dresden`
- `Gioacchino Rossini :: Panorama - Gioacchino Rossini - Disc 1.0x9b10f20b`
- `Gioacchino Rossini :: Panorama.0xe6120d0f`
- `Igor Stravinsky - Columbia Symphony Orchestra :: Stravinsky Conducts - The Firebird Suite - Petrushka Ballet Suite.0x6c0c8818`
- `Jane Austen Entertains :: Music from Her Own Library.0x100ff515`
- `Joshua Bell - Romance of the Violin`
- `Kathleen Battle, Placido Domingo, Metropolitan Opera Orchestra - James Levine :: Live in Tokyo 1988 - Kathleen Battle, Placido Domingo, Metropolitan Opera Orchestra - James Levine.0x660e0809`
- `Marvin Goldstein :: Inspirational Notes.0x9708d90c`
- `Mascagni, Pietro: :: Mascagni: Karajan the Opera Recordings; Karajan::Orch Del Teatro alla Scala.0x2f12eb16`
- `Merill Jensen :: Come Unto Christ - The Conversion of Alma the Younger.0xc20f550e`
- `Michael Bolton :: Time, Love & Tenderness.0x880a820a`
- `Mormon Tabernacle Choir feat. Santino Fontana and The Muppets from Sesame Street - Keep Christmas With You`
- `Mormon Tabernacle Choir, Orchestra at Temple Square, Mack Wilberg - Praise to the Man`
- `Music Together - Bongos (Family Edition 2023)`
- `Music Together - Tambourine (Family Edition 2022)`
- `ProVocal Volume 12: Ella Fitzgerald.0xf60c8711`
- `Red and Yellow.0x870a410a`
- `Reid Nibley :: Sabbath Song.0x11118d12`
- `Schubert String Quintet - Brandis Quartet, Jorg Baumann.Apex.Teldec.0x2a0cfb04`
- `Schubert String Quintet I, II - Piacevole Quintet.0x39071b05`
- `Schubert; Takács Quartet, Ralph Kirshbaum - String Quintet, D. 956 _ String Quartet, D. 703 “Quartettsatz”`
- `Schumann, Robert: :: Schumann: Con for Pno::Con for Clo; Gieseking::Machula::BPO::Furtwangler.0x570c5306`
- `Strauss, Richard: :: Strauss: Sinfonia Domestica::Don Juan; Furtwangler::BPO.0x4d0e0b06`
- `Stravinsky, Igor (1882-1971) :: Le Sacre du Printemps (Stravinsky).0xd4084d0f`
- `The Choirs and Orchestra of Brigham Young University :: Songs of Praise and Remembrance.0xd10d400f`
- `The Feel Good CD.0xd90a370d`
- `Unknown Artist - aKkt9ShtSOg1Acgk8ealeqP6NqE-`
- `Unknown Artist - qlSQ2jDIvQP22d9x8JFZAo5Om4Y-`
- `Various :: Jazz Classic, 10th ED, CD03.0xb912740c`
- `Various :: Jazz Classics, 10th ED, CD01.0xd712ac1f`
- `Various :: Jazz Classics, 10th ED, CD02.0xa912340c`
- `Various :: Preludes, Fugues and Riffs - Jazz in Classical Music.0xab12490d`
- `Various :: Spirit Of The 60's [Blue}.0x6206170a`
- `Various :: Symphony No. 3- Organ.0x5b0e2b07`
- `Various :: The Irving Berlin 100th Anniversary Collection`
- `Wagner, Richard: :: Wagner: Ring Des Nibelungen; Karajan::BPO.0x6b0a9b08`
- `Wagner, Richard: :: Wagner: Ring Des Nibelungen; Karajan::BPO.0xaf11030d`
- `Wilhelm Furtwängler :: [Disc 2] Johannes Brahms.0x1a0b2104`
- `Wilhelm Furtwängler :: [Disc 3] Anton Bruckner.0x2f0fe904`
- `Wilhelm Furtwängler :: [Disk 5] Sibelius, Strauss, Ravel.0x180c3b03`

### whipper / in-mb-mismatch (5 dirs)

- `Lifescapes :: Celtic Christmas.0xcf0c250e`
- `Various :: Spirit Of The 60's [Green].0x6705a60a`
- `Wagner, Richard: :: Wagner: Das Rheingold.0x56123b18`
- `Wagner, Richard: :: Wagner: Das Rheingold; Karajan::BPO.0x450ff514`
- `Wolfgang Amadeus Mozart :: Complete Mozart Edition, Vol. 6: Dances & Marches, Disc 1.0xdd0f3323`

### whipper / not-in-mb (5 dirs)

- `Dave Eaton Show Live.0xc50f780d`
- `Durham Mus Theater.0x43113c15`
- `Karen #4.0xd00f0610`
- `Karen Song #1.0x6f07de09`
- `Ric's Class Music.0x9812024d`

### whipper / non-classical-other (5 dirs)

- `Aesop_Fables.0xe60a3e11`
- `Disc Cleaner.0x720f9e07`
- `Education`
- `Kidz Bop Kids :: Kidz Bop 33.0x990a330e`
- `Various Artists :: Kidz Bop 37.0xd60a640f`

### amazon / non-classical-other (1 dirs)

- `Amazon Music`

### other-download / in-mb-clean (19 dirs)

- `4757765 - Berlioz Requiem`
- `4870712 - Gould Spirituals; Fall River Legend; Barber Medea`
- `6872862 - Samuel Barber Adagio`
- `9029661011 - Itzhak Perlman Plays Tchaikovsky`
- `American Composers Choral Festibal: An Evening with Mack J. Wilberg`
- `Elgar Cello Concerto, Enigma Variations, Pomp and Circumstance March No.1, No.4 - Maisky - Sinopoli - Philharmonia Orchestra [4783619]`
- `Françaix - L’Horloge de Flore.999779-2`
- `Glinka & Glazunov - Chamber Music`
- `Grieg Complete Orchestral Works`
- `Handel The Messiah - Wilberg - Orchestra and Choir at Temple Square`
- `Humperdinck - Hansel und Gretel`
- `Mussorgsky Sorochintsy Fair`
- `Prokofiev Peter and the Wolf, Op. 67`
- `RCID15847740 - Tchaikovsky Complete Romances`
- `Saint-Saens: Carnival of the Animals, Organ Symphony & other orchestral works`
- `Saint-Saëns Symphonies - Martinon - Orchestre National de l’ORTF.6318042`
- `Shostakovich Selected Symphonies, Песнь о лесах - Mravinsky - Санкт-Петербургская филармония`
- `Shostakovich Symphony No.4, No.5, No.6 - Mäkelä - Oslo-Filharmonien [4854637]`
- `Stravinsky - Orchestral Works`

### other-download / in-mb-mismatch (4 dirs)

- `Daniel Barenboim & Orchestre de Paris`
- `Glazunov Complete Symphonies.19642a`
- `Mussorgsky: Romances and Songs`
- `Shostakovich Symphony No.1-15, October, Над Родиной нашей солнце сияет, Казнь Степана Разина, Violin Concerto No.2 [RCID18056928]`

### unknown / not-in-mb (1 dirs)

- `LDS Youth Music`

### unknown / non-classical-other (8 dirs)

- `Audiobooks`
- `Caro mio ben`
- `GarageBand`
- `Into The Woods Piano Accompaniment`
- `Lydia Ballet Exercises`
- `Lydia Dance Repertoire Music`
- `Playlists`
- `nachtmusick`

## Adjudication Log

Pass 2 resolved all 131 axis2=unknown dirs via MB network lookups. 18 dirs received manual adjudication corrections.

9 dirs retain axis1=unknown (no provenance signal found); all are explicitly user-adjudicated below.

| Dir | Axis 1 | Axis 2 | Adjudication notes |
| --- | --- | --- | --- |
| `Audiobooks` | unknown | non-classical-other | User: will manually move out of library; axis1=unknown (no provenance signal); adjudicated 2026-07-2 |
| `Caro mio ben` | unknown | non-classical-other | User: will move out of library; single MP3 of classical aria, not a full release; adjudicated 2026-0 |
| `GarageBand` | unknown | non-classical-other | User: will manually move out of library; axis1=unknown (GarageBand project files); adjudicated 2026- |
| `Into The Woods Piano Accompaniment` | unknown | non-classical-other | User: will move out; piano accompaniment tracks for musical theater rehearsal, not classical; adjudi |
| `LDS Youth Music` | unknown | not-in-mb | User: personal/religious LDS youth music collection, not in MB as a unit; adjudicated 2026-07-20; Us |
| `Lydia Ballet Exercises` | unknown | non-classical-other | User: will manually move out of library; axis1=unknown (no provenance signal); adjudicated 2026-07-2 |
| `Lydia Dance Repertoire Music` | unknown | non-classical-other | User: will move out; personal dance competition music (YAGP submission files), not classical; adjudi |
| `Playlists` | unknown | non-classical-other | User: playlist files only, no audio content; adjudicated 2026-07-20; User: playlist files to be tran |
| `nachtmusick` | unknown | non-classical-other | User: will move out; personal classical MP3 collection folder (79 tracks incl. Messiah subdir), not  |

## Summary Statistics

- Total dirs: 147
- Axis 1 distribution: {'amazon': 1, 'other-download': 23, 'presto': 46, 'unknown': 9, 'whipper': 68}
- Axis 2 distribution: {'already-ingested': 1, 'in-mb-clean': 107, 'in-mb-mismatch': 18, 'non-classical-other': 15, 'not-in-mb': 6}
- Already-ingested (delete-candidates): 1
- Residual unknown axis1 (user-adjudicated): 9
