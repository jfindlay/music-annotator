# census-impl.md — De-Facto Rulings Inventory (S2)

**Sub-track:** V1a (source mining — styleguide arc)
**Session:** S2 — Mine the implementation into the de-facto rulings inventory
**Source:** `src/music_annotator/` — all modules read in full

---

## Coverage KAT

**Completeness claim:** Every editorial choice site named in the S2 roadmap target list is inventoried
below with (layer, case-ID, deliberate|accidental verdict, source location, ratify/overturn recommendation).
The enacted `ARTIST` grammar is captured as REND-1 evidence in Part 3.

**Target list coverage:**

| Target | Covered in | Case-IDs |
|---|---|---|
| Role-classification heuristics | Part 1 | ONT-8, ONT-9, ONT-10, SEL-17, SEL-18 |
| Credit orderings | Part 2 | REND-14, REND-15, REND-16 |
| Separators | Part 3 | REND-17, REND-18 |
| Composite-tag sources | Part 4 | REND-1 (evidence), REND-19, REND-20, REND-21 |
| Path-grammar components | Part 5 | REND-22, REND-23, REND-24, REND-25, REND-26 |
| Concerto path-injection (`_tags.py:1189`) | Part 6 | SEL-11 (evidence), SEL-19 |
| Frozen C-CLASS / C-INIT shapes | Part 7 | (validate-only; findings in Discoveries) |
| Enacted `ARTIST` grammar (`_pipeline.py:1742`) | Part 3 | REND-1 (evidence) |

**Honest gaps:** The `_pipeline_io.py` collision-resolution and `_discover.py` search-ranking logic
are not editorial choices in the styleguide sense (they are operational/identity machinery). They are
noted in the Discoveries section where they surface styleguide-adjacent signals.

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
**This session mints:** ONT-8..10, SEL-17..19, NORM-8..9, REND-14..26, EPIST-7..8.

---

## Part 1 — Role-Classification Heuristics

### 1.1 Ensemble identification by name substring

**Source:** `_artists.py:13–48` (`ORCHESTRA_STRINGS`, `CHOIR_STRINGS`, `GROUP_STRINGS`,
`ENSEMBLE_STRINGS`); `_artists.py:79–88` (`is_ensemble`); `_tags.py:453–465` (`build_cea_performers`
performer-vs-ensemble branch).

**Enacted rule:** A performer is classified as an ensemble (routed to `cea.ensembles`) if their
display name contains any substring from `ENSEMBLE_STRINGS` (case-insensitive). The union set covers
orchestras (`"orchestra"`, `"philharmonic"`, `"philharmoniker"`, `"musicians"`, `"academy"`,
`"symphony"`, `"orkester"`, `"philharmonica"`), choirs (`"choir"`, `"chorus"`, `"singers"`,
`"domchor"`, `"koor"`, `"kammerkoor"`), and chamber groups (`"ensemble"`, `"band"`, `"trio"`,
`"quartet"`, `"quintet"`, `"sextet"`, `"septet"`, `"octet"`, `"chamber"`, `"consort"`, `"players"`,
`"quartett"`). All other performers are classified as soloists (instrumentalists, vocalists, or
other_soloists).

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| ONT | ONT-8 (mint) | **deliberate** | `_artists.py:13–48` | Ratify: the substring vocabulary is a direct port of CE's `cea_orchestras`/`cea_choirs`/`cea_groups` defaults (D-S1-6 in census-ce.md). The vocabulary is frozen as a `frozenset` constant — an intentional design choice. |

**ONT-8 — Ensemble identification vocabulary:** The closed vocabulary of name-substrings that
classify a performer as an ensemble rather than a soloist. Deliberate: the vocabulary is a named
constant (`ENSEMBLE_STRINGS`) with explicit CE provenance. V1b: ratify the vocabulary as the
implementation of CE's ensemble-classification mechanism; note that it is a substring match (not
word-boundary), so `"bandmaster"` would match `"band"` — a known edge case to document.

---

### 1.2 Vocal vs. instrumental soloist classification

**Source:** `_tags.py:459–465` (`build_cea_performers`, vocal-keyword branch).

**Enacted rule:** Within the non-ensemble performer branch, a soloist is classified as a vocalist
if their instrument label (first `attribute-list` entry) contains any of: `"soprano"`, `"mezzo"`,
`"tenor"`, `"baritone"`, `"bass"`, `"contralto"`, `"voice"`, `"vocal"`, `"singer"`. Otherwise, if
the instrument label is non-empty, they go to `instrumentalists`; if empty, to `other_soloists`.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| ONT | ONT-9 (mint) | **deliberate** | `_tags.py:459–465` | Ratify: the vocal-keyword list is a direct CE convention. The three-way split (vocalist / instrumentalist / other_soloist) is the CE `cea_*` bucket structure. |

**ONT-9 — Vocal-keyword classification:** The closed vocabulary of instrument-label substrings that
route a soloist to the `vocalists` bucket. Deliberate: the list is inline but clearly intentional
(CE-derived). V1b: ratify; note that `"bass"` matches both bass voice and bass instrument — a known
ambiguity in the CE model.

---

### 1.3 Additional/assistant composer routing

**Source:** `_works.py:253–260` (`extract_work_artist_rels`, composer-attribute branch).

**Enacted rule:** Work-level composer relations that carry the MB `"additional"` or `"assistant"`
attribute are routed to `role_buckets.additional_composers` rather than `role_buckets.composers`.
This is an extension beyond CE (noted in the docstring: "extension beyond Classical Extras").

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| ONT | ONT-10 (mint) | **deliberate** | `_works.py:253–260` | Ratify: the docstring explicitly calls this an extension. The motivation (distinguishing Süssmayr-type completions from primary composers for directory naming) is documented. |

**ONT-10 — Additional/assistant composer distinction:** Routing MB `"additional"`/`"assistant"`
composer relations to a separate bucket. Deliberate extension beyond CE. V1b: ratify; this is the
implementation of SEL-8 (completers and orchestrators) for the directory-naming case.

---

### 1.4 Recording-level relation type routing

**Source:** `_tags.py:435–467` (`build_cea_performers`, `match rel.type` block).

**Enacted rule:** The `match/case` block routes MB relation types to CE buckets:
- `"conductor"` → `conductors`
- `"chorus master"` → `chorusmasters`
- `"concertmaster"` → `leaders`
- `"arranger"` | `"instrument arranger"` | `"vocal arranger"` → `arrangers`
- `"orchestrator"` → `orchestrators`
- `"composer"` | `"writer"` → `composers` (recording-level; CE merges both)
- `"producer"` → `producers`
- `"balance"` | `"engineer"` | `"mix"` | `"recording"` | `"audio"` | `"sound"` → `engineers`
- `"performer"` | `"instrument"` | `"vocal"` | `"performing orchestra"` → ensemble/soloist branch

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| ONT | SEL-17 (mint) | **deliberate** | `_tags.py:435–467` | Ratify: the routing table is a direct CE convention. The `"writer"` → `composers` merge is explicitly commented ("CE merges both into composer host tag"). |

**SEL-17 — Recording-level relation-type routing table:** The mapping from MB relation types to
CE performer buckets. Deliberate: the table is a direct CE port with explicit comments. V1b: ratify;
note that `"writer"` is merged into `composers` (a CE convention that may need documentation in
the styleguide).

---

### 1.5 Work-level relation type routing

**Source:** `_works.py:253–283` (`extract_work_artist_rels`, `match rel.type` block).

**Enacted rule:** Work-level relations are routed to `role_buckets`:
- `"composer"` (with additional/assistant attribute) → `additional_composers`
- `"composer"` (plain) → `composers`
- `"writer"` → `writers` (distinct from recording-level where it merges into `composers`)
- `"lyricist"` → `lyricists`; `"librettist"` → `librettists`; `"translator"` → `translators`
- `"arranger"` | `"instrument arranger"` | `"vocal arranger"` → `arrangers`
- `"orchestrator"` → `orchestrators`
- `"reconstructed by"` → `reconstructors`; `"revised by"` → `revisors`
- `"adapter"` → `arrangers` (treated as arranger; commented)
- `"dedication"` → `dedicatees`; `"choreographer"` → `choreographers`

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| ONT | SEL-18 (mint) | **deliberate** | `_works.py:253–283` | Ratify: the routing table is deliberate. Note the divergence from recording-level: `"writer"` goes to `writers` at work level but merges into `composers` at recording level. |

**SEL-18 — Work-level relation-type routing table:** The mapping from MB work relation types to
`RoleBuckets`. Deliberate. V1b: ratify; document the `"writer"` divergence between work-level
(own bucket) and recording-level (merged into composers) — this is a CE-continuity question.

---

## Part 2 — Credit Orderings

### 2.1 Performer ordering in composite tags

**Source:** `_tags.py:749–752` (`build_track_tags`, `cea_recording_artist` assembly).

**Enacted rule:** `CEA_RECORDING_ARTIST` is assembled as:
`[all_soloists] + [ensembles] + [conductors]` (soloists first, then ensembles, then conductors).
The `"; "` separator joins names within each category; the categories are concatenated in that order.
Fallback: `rec_artist_phrase` (MB recording credit verbatim) when the assembled list is empty.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-14 (mint) | **deliberate** | `_tags.py:749–752` | Ratify: the soloists→ensembles→conductors order is the CE spine order (STYLEGUIDE Layer 1 working spine). The fallback to `rec_artist_phrase` is also deliberate (CE `cea_mb_artists` pattern). |

**REND-14 — `CEA_RECORDING_ARTIST` assembly order:** soloists → ensembles → conductors, "; "-joined,
with `rec_artist_phrase` fallback. Deliberate. V1b: ratify as the enacted CE spine order.

---

### 2.2 Path performers ordering (album-level conductors + ensembles)

**Source:** `_tags.py:1160–1172` (`build_dest_path`, performers-component assembly).

**Enacted rule:** The path performers component is assembled as:
`album_conductors + album_ensembles` (conductors first, then ensembles), "; "-joined.
Fallback chain: per-track union of all conductors + ensembles → `CEA_ENSEMBLE_NAMES` → `ARTIST`.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-15 (mint) | **deliberate** | `_tags.py:1160–1172` | Ratify: conductors-before-ensembles in the path is a deliberate choice (the conductor is the primary identity signal for a classical recording). However, this diverges from the tag order (soloists first in REND-14). The path order is conductor-first; the tag order is soloist-first. |

**REND-15 — Path performers ordering:** conductors before ensembles in the path component. Deliberate.
V1b: note the inversion relative to REND-14 (tags: soloists first; path: conductors first). This is
a coherence question for P1 (cross-surface coherence) — the path and tag orderings differ. Surface
for V1b adjudication.

---

### 2.3 Concerto path ordering (soloists prepended)

**Source:** `_tags.py:1189–1190` (`build_dest_path`, concerto-soloist injection).

**Enacted rule:** For Concerto works, `cea_album_soloists_unified` is prepended to the performers
component: `"<soloists>; <conductor/ensemble>"`. Soloists come first in the path for concertos.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-16 (mint) | **deliberate** | `_tags.py:1189–1190` | Ratify: the docstring explicitly calls this "soloist-first is the natural CE ordering for a concerto recording". Deliberate. |

**REND-16 — Concerto path soloist-first ordering:** soloists prepended before conductor/ensemble in
the path performers component for Concerto works. Deliberate. V1b: ratify; this is the enacted
resolution of the concerto-soloist ordering question.

---

## Part 3 — Separators

### 3.1 Intra-list separator: `"; "`

**Source:** `_tags.py:585–605` (`build_cwp_tags`, composer/arranger joins); `_tags.py:712–747`
(`build_track_tags`, all performer string joins); `_tags.py:1163` (path performers join).

**Enacted rule:** All multi-value lists within a single tag field are joined with `"; "` (semicolon-space).
This applies to: `cwp_composers`, `cwp_arrangers`, `conductor`, `soloists`, `ensemble`,
`cea_recording_artist`, path performers, and all other `"; ".join(...)` calls throughout.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-17 (mint) | **deliberate** | `_tags.py:585–747` (pervasive) | Ratify: `"; "` is the CE convention for multi-value separator within a single tag field. Consistent throughout. |

**REND-17 — Intra-list separator `"; "`:** All multi-value lists within a tag field use `"; "`.
Deliberate CE convention. V1b: ratify.

---

### 3.2 Work-hierarchy separator: `" :: "`

**Source:** `_tags.py:569` (`build_cwp_tags`, `groupheading` assembly: `" :: ".join(gh_parts)`);
`_tags.py:574` (`cwp_inter_work` assembly).

**Enacted rule:** The `groupheading` tag joins work title and part titles with `" :: "` (space-colon-colon-space).
This is the CE `groupheading` separator convention (confirmed by D-S1-9 in census-ce.md).

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-18 (mint) | **deliberate** | `_tags.py:569,574` | Ratify: `" :: "` is the CE `groupheading` separator. Consistent with D-S1-9. |

**REND-18 — Work-hierarchy separator `" :: "`:** `groupheading` and `cwp_inter_work` use `" :: "`.
Deliberate CE convention. V1b: ratify.

---

### 3.3 Composer–performer path separator: `" - "`

**Source:** `_tags.py:1292` (`build_dest_path`, `safe_name(f"{composer} - {performers}")`);
`_tags.py:1303` (non-classical `artist_part - album_part`); `_proposed_short:180` (strategy 3
identifies `" - "` as the composer–performer separator for truncation).

**Enacted rule:** The top-level directory component uses `" - "` (space-dash-space) to separate the
composer (or album artist) from the performers (or album title). This is the primary path separator.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-17 | (maps to existing) | `_tags.py:1292` | — |

Note: `" - "` is a distinct separator from `"; "` and serves a different structural role (composer/performer
boundary vs. intra-list). It is already partially covered by REND-2 (census-ce.md: "Composer-last-name
prefix on album title"). No new case minted; evidence added to REND-2.

---

## Part 4 — Composite-Tag Sources

### 4.1 `ARTIST` tag — enacted grammar (REND-1 evidence)

**Source:** `_tags.py:840` (`build_track_tags`: `artist=rec_artist_phrase`);
`_pipeline.py:1742` (`run` minimal-tags branch: `artist=artist_credit_phrase(track.recording.artist_credit)`).

**Enacted rule:** The `ARTIST` tag is set to the **MB recording artist-credit phrase verbatim** in
both the full-tags path (`build_track_tags`) and the minimal-tags path (`run` else-branch). The
recording artist credit is the raw MB `artist-credit` list reconstructed by `artist_credit_phrase`
— it is the credited name(s) on the recording, not a CE-processed composite.

**REND-1 evidence (enacted grammar):**
- Full-tags path: `artist = rec_artist_phrase` = `artist_credit_phrase(rec.artist_credit)` where
  `rec` is the recording stub on the track. This is the MB recording credit verbatim.
- Minimal-tags path (`_pipeline.py:1742`): `artist = artist_credit_phrase(track.recording.artist_credit)`.
  Same source, same result.
- The `ARTIST` tag does **not** lead with the composer in the implementation. The composer is in
  `COMPOSER` (a separate tag). The `ARTIST` field carries performers only (the MB recording credit).
- `CEA_RECORDING_ARTIST` (REND-14) is the CE-processed composite (soloists + ensembles + conductors);
  `ARTIST` is the raw MB credit. These are distinct tags with distinct semantics.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-1 (E0, evidence) | **deliberate** | `_tags.py:840`; `_pipeline.py:1742` | The implementation resolves REND-1 as "performers only, MB recording credit verbatim". This is a de-facto ruling. V1b: adjudicate REND-1 against this evidence plus CE evidence (D-S1-2: CE uses performer-first with composer as fallback). The implementation is more conservative than CE (no composer fallback into ARTIST). |

---

### 4.2 `ALBUMARTIST` tag source

**Source:** `_tags.py:843` (`albumartist=album_artist_phrase`); `album_artist_phrase =
artist_credit_phrase(release.artist_credit)`.

**Enacted rule:** `ALBUMARTIST` is the MB release artist-credit phrase verbatim.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-19 (mint) | **deliberate** | `_tags.py:843` | Ratify: MB release artist credit is the natural source for ALBUMARTIST. Consistent with Picard convention. |

**REND-19 — `ALBUMARTIST` source:** MB release artist-credit phrase verbatim. Deliberate. V1b: ratify.

---

### 4.3 `COMPOSER` tag source (work-level vs. recording-level fallback)

**Source:** `_tags.py:699–710` (`build_track_tags`, composer derivation).

**Enacted rule:** `COMPOSER` is derived in priority order:
1. Work-level `role_buckets.composers` (plain primary composer relations from the work hierarchy).
2. Work-level `role_buckets.additional_composers` (fallback when no plain primary composer).
3. Recording-level `cea.composers` (fallback when no work-level composer at all).

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| SEL | SEL-19 (mint) | **deliberate** | `_tags.py:699–710` | Ratify: the three-level fallback is deliberate (documented in the comment). The work-level-first priority is the correct CE convention. |

**SEL-19 — `COMPOSER` source priority:** work-level primary → work-level additional → recording-level.
Deliberate. V1b: ratify; this is the enacted resolution of the composer-source question for the
`COMPOSER` tag.

---

### 4.4 `GENRE` tag source

**Source:** `_tags.py:804–805` (`build_track_tags`: `wtype_genre = WORKTYPE_GENRES.get(cwp.worktype_genres, "")`;
`genre = wtype_genre or "Classical"`).

**Enacted rule:** `GENRE` is derived from the MB work type via `WORKTYPE_GENRES` lookup. If the work
type is not in the map (or no work hierarchy), defaults to `"Classical"`. The `WORKTYPE_GENRES` map
covers: Symphony, Concerto, Opera, Oratorio, Cantata, Mass, Motet, Ballet, Symphonic poem, Suite,
Overture, Chamber music, Sonata, Song cycle, Choral, Partita, Aria.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-20 (mint) | **deliberate** | `_tags.py:804–805`; `_works.py:32–50` | Ratify: the WORKTYPE_GENRES map is a CE convention. The `"Classical"` default is deliberate (CE `is_classical` flag). |

**REND-20 — `GENRE` source:** MB work type → `WORKTYPE_GENRES` map → `"Classical"` default.
Deliberate CE convention. V1b: ratify.

---

### 4.5 `IS_CLASSICAL` flag

**Source:** `_tags.py:906` (`build_track_tags`: `is_classical="1"`); `models.py:1358`
(`TrackTags.is_classical: str = "1"` — default value).

**Enacted rule:** `IS_CLASSICAL` is always `"1"` for all tracks processed by `build_track_tags`.
The field defaults to `"1"` in `TrackTags` and is never set to anything else in the full-tags path.
In the minimal-tags path (`_pipeline.py:1735–1754`), `IS_CLASSICAL` is not set (defaults to `"1"`
from the model).

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-21 (mint) | **accidental** | `_tags.py:906`; `models.py:1358` | Overturn candidate: `IS_CLASSICAL="1"` is hardcoded for all tracks regardless of the `_top_level_class` result. A non-classical release (Popular, Spoken Word, etc.) processed by `build_track_tags` will still carry `IS_CLASSICAL="1"`. This is likely accidental — the flag should reflect the actual class. However, `build_track_tags` is only called for classical releases in the current pipeline (non-classical releases use the minimal-tags path). So the bug is latent, not active. V1b: ratify the CE convention (`IS_CLASSICAL="1"` for classical); add a note that the flag should be conditional on `_top_level_class` if `build_track_tags` is ever called for non-classical releases. |

**REND-21 — `IS_CLASSICAL` hardcoded to `"1"`:** Accidental in the general case; currently harmless
because `build_track_tags` is only called for classical releases. V1b: note the latent bug.

---

## Part 5 — Path-Grammar Components

### 5.1 Top-level class component (C-CLASS)

**Source:** `_tags.py:227–275` (`_top_level_class`); `_tags.py:1280` (`class_dir = safe_name(_top_level_class(tags))`).

**Enacted rule (C-CLASS, frozen at S1):** Six-way routing from embedded tags:
1. `releasetype_secondary` contains Audiobook/Spokenword/Audio drama/Interview → `"Spoken Word"`
2. `releasetype_secondary` contains Soundtrack → `"Soundtracks"`
3. `cwp_work_top` non-empty AND `cwp_worktype_genres_top` contains `"Classical"` → `"Classical"`
4. `releasetype_secondary` contains Compilation (non-classical) → `"Compilations"`
5. `releasetype` in {Album, Single, EP, Broadcast, Other} → `"Popular"`
6. No signal → `"Unsorted"`

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-22 (mint) | **deliberate** | `_tags.py:227–275` | Validate-only (C-CLASS frozen). Evidence recorded. |

**REND-22 — Top-level class routing (C-CLASS):** Six-way routing from embedded tags. Frozen contract.
V1b: validate only; any apparent conflict is a C-CLASS/C-INIT finding (see Discoveries).

---

### 5.2 Within-classical top-dir component (C-INIT)

**Source:** `_tags.py:278–340` (`_classical_top_dir`); `_tags.py:1291–1292` (caller).

**Enacted rule (C-INIT, frozen at S2):** Three cases evaluated in order:
1. **Compilation** (`releasetype_secondary` contains `"Compilation"`): `<albumartist_last_name or "Various"> - <album>`.
2. **Recital** (both `CWP_COMPOSER_LASTNAMES` and `CEA_COMPOSER_LASTNAMES` empty): `<albumartist> - <album>`.
3. **Single-composer** (dominant): returns `None` → caller uses `<composer> - <performers>`.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-23 (mint) | **deliberate** | `_tags.py:278–340` | Validate-only (C-INIT frozen). Evidence recorded. The docstring notes a CE divergence for recitals: CE uses composer-first even for recitals when a composer can be inferred; the implementation uses album artist when no composer is linked in MB. |

**REND-23 — Within-classical top-dir (C-INIT):** Three-case routing. Frozen contract. CE divergence
noted (recital case). V1b: validate only; the CE divergence is documented in the docstring.

---

### 5.3 Work-dir component (title + year suffix)

**Source:** `_tags.py:1204–1254` (`build_dest_path`, work-dir assembly).

**Enacted rule:** Work dir = `safe_name(work_title)` where `work_title` is `CWP_WORK_TOP` → `WORK`
→ `ALBUM` → `"Unknown Album"`. Year suffix priority:
- `[rec YYYY]` from `recording_date_work` (work-level union) or `RECORDING_DATE` (per-track).
- `[rel YYYY]` from `RECORDING_FIRST_RELEASE_DATE` → `ORIGINALDATE` → `DATE`.
- No suffix when no date available.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-24 (mint) | **deliberate** | `_tags.py:1204–1254` | Ratify: the `[rec YYYY]` vs `[rel YYYY]` distinction is the enacted realisation of STYLEGUIDE 5.3 (rendered, not buried — the basis of the date claim is visible in the path). The priority chain is deliberate. |

**REND-24 — Work-dir year suffix:** `[rec YYYY]` preferred over `[rel YYYY]`; basis visible in path.
Deliberate realisation of STYLEGUIDE 5.3. V1b: ratify.

---

### 5.4 Leaf `nn` prefix and fallback chain

**Source:** `_tags.py:1325–1336` (deep hierarchy); `_tags.py:1344–1352` (1–2 level hierarchy).

**Enacted rule:** Leaf `nn` prefix uses: `CWP_MOVT_NUM` (per-group gap-free index, set by
`_apply_workgroup_unification`) → `global_track_idx` (1-based copy-subset position) → `track.position`.
Width: 3 digits when `MOVEMENTTOTAL > 99`, else 2 digits.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-25 (mint) | **deliberate** | `_tags.py:1325–1352` | Ratify: the fallback chain is deliberate (documented in comments). `CWP_MOVT_NUM` is the primary authority; `global_track_idx` prevents collisions on multi-disc works. |

**REND-25 — Leaf `nn` fallback chain:** `CWP_MOVT_NUM` → `global_track_idx` → `track.position`.
Deliberate. V1b: ratify.

---

### 5.5 Intermediate directory `nn` prefix (C-L1)

**Source:** `_tags.py:1315–1323` (intermediate directory assembly); `_pipeline.py:905–969`
(`_apply_workgroup_unification`, C-L1 pass).

**Enacted rule:** Intermediate directory `nn` uses `CWP_INTER_INDEX_{i}` (gap-free sibling rank
computed by `_apply_workgroup_unification`) → `CWP_ORDERING_KEY_{i}` (raw MB ordering key) → `i`
(level index as fallback). Siblings are ranked by ascending `cwp_ordering_key_{i}`, ties broken by
first-appearance order.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-26 (mint) | **deliberate** | `_tags.py:1315–1323`; `_pipeline.py:905–969` | Ratify: the C-L1 contract is deliberate (documented). The gap-free sibling-rank approach mirrors the leaf `CWP_MOVT_NUM` pattern. |

**REND-26 — Intermediate `nn` fallback chain:** `CWP_INTER_INDEX_{i}` → `CWP_ORDERING_KEY_{i}` → `i`.
Deliberate. V1b: ratify.

---

## Part 6 — Concerto Path-Injection (SEL-11 evidence)

**Source:** `_tags.py:1174–1190` (`build_dest_path`, concerto-soloist injection block).

**Enacted rule:** When `CWP_WORKTYPE_GENRES_TOP == "Concerto"` AND `tags.cea_album_soloists_unified`
is non-empty, the soloists string is prepended to the performers component:
`performers = f"{tags.cea_album_soloists_unified}; {performers}"`.

The `cea_album_soloists_unified` value is the cross-medium union of `cea_album_soloists` (falling
back to `cea_soloists`) across all movements of the top work, computed by
`_apply_workgroup_unification`'s soloist-union pass (`_pipeline.py:1087–1101`).

**Gate:** Strictly `top_work.type == "Concerto"` (via `CWP_WORKTYPE_GENRES_TOP`). The docstring
explicitly states: "The gate is strictly `top_work.type == "Concerto"`; other canonical-soloist
work types are deferred (see plan appendix)."

**SEL-11 evidence (coherence-violation observation):**
SEL-11 asks: "When is a soloist part of the work's canonical identity (and thus promoted into compact
projections) beyond the mechanical concerto case?" The implementation answers this question for
exactly one work type (`"Concerto"`) and defers all others. This is a coherence violation in
miniature: the gate is type-specific rather than principle-derived. The principle (a soloist is part
of the canonical identity when the work is *for* that soloist) applies to organ symphonies,
symphony-with-obbligato, and other cases — but the implementation only acts on `"Concerto"`.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| SEL | SEL-11 (E0, evidence) | **deliberate** (gate) / **accidental** (scope) | `_tags.py:1174–1190` | The gate itself is deliberate (the docstring calls it "the ONLY mechanical case in scope"). The scope limitation (Concerto only) is a known deferral, not an accident. V1b: adjudicate SEL-11 using this evidence — the implementation provides a concrete, working example of canonical-soloist promotion; the generalisation question is open. |

---

## Part 7 — Frozen C-CLASS / C-INIT Shapes (Validate-Only)

### 7.1 C-CLASS vocabulary

**Source:** `_tags.py:222–224` (`_CLASS_VOCAB: frozenset[str] = frozenset({"Spoken Word", "Soundtracks",
"Classical", "Compilations", "Popular", "Unsorted"})`).

**Enacted shape:** Six closed values. Used by `_top_level_class`, `_work_dir_component`,
`_work_top_dir`, `_apply_collision_suffix`, and `_pipeline_io.py` (imported as `_CLASS_VOCAB`).

**Validation:** The vocabulary is consistent across all call sites. No apparent conflict with the
C-CLASS contract. Evidence recorded.

---

### 7.2 C-INIT shapes

**Source:** `_tags.py:278–340` (`_classical_top_dir`).

**Enacted shapes:**
- Compilation: `<albumartist_last_name or "Various"> - <album>`
- Recital: `<albumartist> - <album>` (when no composer linked in MB)
- Single-composer: `None` → caller uses `<composer> - <performers>`

**Validation:** The three cases are consistent with the C-INIT docstring. The recital case has a
documented CE divergence (noted in the docstring). No apparent conflict with the C-INIT contract.
Evidence recorded.

---

## Part 8 — Additional Enacted Choices (not in roadmap target list)

### 8.1 Arranger annotation format

**Source:** `_tags.py:718–735` (`build_track_tags`, arranger string assembly).

**Enacted rule:** Arrangers from `role_buckets.orchestrators` are annotated with `" (orch.)"`;
reconstructors with `" (reconstructed)"`; revisors with `" (revised)"`. Recording-level arrangers
and work-level plain arrangers are unannotated. The annotation is appended in parentheses after the
name.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-3 (S1 mint, evidence) | **deliberate** | `_tags.py:718–735` | Maps to REND-3 (role-annotation text within host tags). Deliberate CE convention. |

---

### 8.2 `groupheading` assembly

**Source:** `_tags.py:555–574` (`build_cwp_tags`, groupheading assembly).

**Enacted rule:** `groupheading` = `work_top :: [intermediate parts] :: bottom_part`, using `" :: "`
separator. When `n_levels == 1`, `groupheading = work_names[0]` (no separator needed). When
`n_levels == 2`, `groupheading = work_top :: bottom_part`.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-18 (evidence) | **deliberate** | `_tags.py:555–574` | Maps to REND-18. Consistent with CE `groupheading` convention (D-S1-9). |

---

### 8.3 Period map (CE default)

**Source:** `_works.py:19–29` (`PERIOD_MAP`).

**Enacted rule:** Nine periods with overlapping year ranges (Early Romantic 1800–1850 overlaps
Classical 1750–1820 and Late Romantic 1850–1910). First-match wins in `period_for_year`.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| NORM | NORM-8 (mint) | **deliberate** | `_works.py:19–29` | Ratify: the period map is a direct CE default. The overlapping ranges (Early Romantic starts before Classical ends) are a CE convention. |

**NORM-8 — Period map and year-range boundaries:** CE default period map with overlapping ranges.
Deliberate CE port. V1b: ratify; note the overlapping ranges are intentional (CE convention).

---

### 8.4 Work-title prefix stripping

**Source:** `_works.py:183–217` (`strip_common_prefix`).

**Enacted rule:** Part title is derived by stripping the parent work title prefix from the child
title (case-insensitive), then stripping leading punctuation/whitespace. Fallback: split on `": "`
(colon-space, not bare colon) to avoid false splits on catalogue numbers like `"Hob. III:31"`.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| NORM | NORM-9 (mint) | **deliberate** | `_works.py:183–217` | Ratify: the `": "` (colon-space) requirement is explicitly documented in the docstring as a deliberate choice to avoid catalogue-number false splits. |

**NORM-9 — Work-title prefix stripping and `": "` separator:** Deliberate. The colon-space
requirement is a documented design choice. V1b: ratify.

---

### 8.5 MP3 tag mapping (`_MP3_STD_KEYS` / `_MP3_TXXX_MAP`)

**Source:** `_tagger.py:44–242` (`_MP3_STD_KEYS`, `_MP3_TXXX_MAP`).

**Enacted rule:** Standard ID3 frames (`TIT2`, `TPE1`, `TPE2`, `TALB`, `TRCK`, `TPOS`, `TDRC`,
`TDOR`, `TCOM`, `TPE3`, `TPUB`, `TSRC`, `TLEN`, `TSST`) carry the standard Picard fields.
All other fields go to `TXXX` frames with `desc == key` (own-namespace convention for CE/CWP tags).
MusicBrainz IDs use the standard Picard TXXX descriptions (e.g. `"MusicBrainz Album Id"`).

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-13 (S1 mint, evidence) | **deliberate** | `_tagger.py:44–242` | Maps to REND-13 (performer sub-tag grammar). The TXXX own-namespace convention is deliberate. |

---

### 8.6 Duplicate-relation suppression

**Source:** `_tags.py:401–427` (`build_cea_performers`, `seen` dict and `_append` helper).

**Enacted rule:** Duplicate MB relations (same artist MBID, same bucket) are silently suppressed.
Entries without an MBID are always appended (MBID-based dedup not possible).

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| SEL | SEL-17 (evidence) | **deliberate** | `_tags.py:401–427` | Maps to SEL-17 (recording-level relation routing). The dedup is deliberate (documented: "occurs in the wild on some DG recordings"). |

---

### 8.7 `WORK` tag source

**Source:** `_tags.py:800–801` (`build_track_tags`: `work_tag = cwp.work_top or _level0_title or
direct_work_title or ""`).

**Enacted rule:** `WORK` tag priority: `cwp.work_top` (top of work hierarchy) → level-0 work title
(when work_top is empty but levels exist) → `direct_work_title` (from first performance relation
stub when no hierarchy was fetched) → `""`.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| REND | REND-5 (S1 mint, evidence) | **deliberate** | `_tags.py:800–801` | Maps to REND-5 (work/movement tag name assignment). The priority chain is deliberate. |

---

### 8.8 Annotation tier classification

**Source:** `models.py:105–127` (`classify_annotation_tier`); `_pipeline.py:1841–1860`
(census-signal derivation).

**Enacted rule:** Five-rung annotation tier ladder: `full-mb-verified` (embedded MBID or TOC
disc-ID match or ISRC match) → `mb-search-resolved` (search-resolved, needs spot check) →
`mb-partial` (track-count mismatch) → `alternate-source` (reserved) → `source-tags-only` (no MB).

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| EPIST | EPIST-7 (mint) | **deliberate** | `models.py:105–127`; `_pipeline.py:1841–1860` | Ratify: the five-rung ladder is a deliberate design (C-TIER contract, frozen at S1). The `needs_spot_check` flag for `mb-search-resolved` is deliberate. |

**EPIST-7 — Annotation tier ladder:** Five rungs from `source-tags-only` to `full-mb-verified`.
Deliberate. V1b: ratify.

---

### 8.9 Primary work selection (multi-performance-link recordings)

**Source:** `_works.py:87–180` (`_score_top_work`, `select_primary_performance_work`).

**Enacted rule:** When a recording is linked to multiple works via `performance` relations, the
primary work is selected by scoring the root of each candidate's `parts/backward` chain:
`+2` for a non-empty MB work type; `+1` for absence of a `based on/backward` relation.
Ties broken by first-appearance order.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| SEL | SEL-16 (S1 mint, evidence) | **deliberate** | `_works.py:87–180` | Maps to SEL-16 (minted in S1 as "recording-artist vs. track-artist merge"). Actually this is a distinct case — primary-work selection. Mint SEL-16 evidence here; the S1 SEL-16 was about recording-artist merge. Check: S1 SEL-16 was "Recording artist vs. track artist: merge vs. replace". This is a different case. Mint new case. |

Correction: S1 SEL-16 was about recording-artist merge. Primary-work selection is a new case.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| SEL | SEL-19 (already minted above for COMPOSER source) | — | — | — |

Mint: this is distinct from SEL-19 (COMPOSER source). Use the next available number.

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| SEL | SEL-20 (mint) | **deliberate** | `_works.py:87–180` | Ratify: the scoring heuristic is deliberate (documented in the docstring as "extension beyond Classical Extras"). The `based on/backward` penalty correctly identifies subsidiary works (cadenza collections). |

**SEL-20 — Primary work selection for multi-performance-link recordings:** Score-based selection
(work type + no-based-on). Deliberate extension beyond CE. V1b: ratify.

---

### 8.10 `EPIST-8` — Provenance sidecar mechanism

**Source:** `models.py:1820–1851` (`ProvenanceSidecar`); `_pipeline.py:1358–1388`
(`_copy_tag_verify_journal_pass`, sidecar write).

**Enacted rule:** Annotation tier and AccurateRip summary are written to a YAML provenance sidecar
(`freedb_disc_N.yaml` or `music_annotator_provenance.yaml`) in the work top directory. The tier
is monotonically upgradeable only. This is the library-level realisation of STYLEGUIDE 5.5
(contested-case marking) and 5.1 (claim and basis).

| Layer | Case-ID | Verdict | Source location | Ratify/Overturn |
|---|---|---|---|---|
| EPIST | EPIST-8 (mint) | **deliberate** | `models.py:1820–1851`; `_pipeline.py:1358–1388` | Ratify: the sidecar mechanism is the library-level partition's realisation of STYLEGUIDE 5.5. The monotonic-upgrade rule is deliberate. |

**EPIST-8 — Provenance sidecar mechanism:** YAML sidecar with annotation tier and AccurateRip
summary, monotonically upgradeable. Deliberate. V1b: ratify as the library-level 5.5 realisation.

---

## Part 9 — Case-ID Registry (this session)

| Case-ID | Layer | Description | Status |
|---|---|---|---|
| ONT-8 | Ontology | Ensemble identification vocabulary (name-substring) | minted |
| ONT-9 | Ontology | Vocal-keyword classification for soloists | minted |
| ONT-10 | Ontology | Additional/assistant composer distinction | minted |
| SEL-17 | Selection | Recording-level relation-type routing table | minted |
| SEL-18 | Selection | Work-level relation-type routing table | minted |
| SEL-19 | Selection | `COMPOSER` tag source priority (work → additional → recording) | minted |
| SEL-20 | Selection | Primary work selection for multi-performance-link recordings | minted |
| NORM-8 | Normalisation | Period map and year-range boundaries (CE default) | minted |
| NORM-9 | Normalisation | Work-title prefix stripping and `": "` separator | minted |
| REND-14 | Rendering | `CEA_RECORDING_ARTIST` assembly order (soloists→ensembles→conductors) | minted |
| REND-15 | Rendering | Path performers ordering (conductors before ensembles) | minted |
| REND-16 | Rendering | Concerto path soloist-first ordering | minted |
| REND-17 | Rendering | Intra-list separator `"; "` | minted |
| REND-18 | Rendering | Work-hierarchy separator `" :: "` | minted |
| REND-19 | Rendering | `ALBUMARTIST` source (MB release credit verbatim) | minted |
| REND-20 | Rendering | `GENRE` source (WORKTYPE_GENRES map → `"Classical"` default) | minted |
| REND-21 | Rendering | `IS_CLASSICAL` hardcoded to `"1"` (latent bug) | minted |
| REND-22 | Rendering | Top-level class routing (C-CLASS, validate-only) | minted |
| REND-23 | Rendering | Within-classical top-dir (C-INIT, validate-only) | minted |
| REND-24 | Rendering | Work-dir year suffix (`[rec YYYY]` vs `[rel YYYY]`) | minted |
| REND-25 | Rendering | Leaf `nn` fallback chain | minted |
| REND-26 | Rendering | Intermediate `nn` fallback chain (C-L1) | minted |
| EPIST-7 | Epistemic | Annotation tier ladder (five rungs) | minted |
| EPIST-8 | Epistemic | Provenance sidecar mechanism (YAML, monotonic-upgrade) | minted |

**Mint count summary (this session):** ONT: 3 (ONT-8..10), SEL: 4 (SEL-17..20), NORM: 2 (NORM-8..9),
REND: 13 (REND-14..26), EPIST: 2 (EPIST-7..8). Total new: 24 cases.

E0 cases with new evidence: REND-1 (ARTIST grammar), SEL-11 (concerto gate), REND-3 (arranger
annotation), REND-5 (WORK tag source), REND-13 (MP3 tag mapping).

S1 cases with new evidence: SEL-16 (primary-work selection — note: S1 SEL-16 was recording-artist
merge; this session's primary-work selection is SEL-20, a distinct case).

---

## Part 10 — Discoveries

**D-S2-1 (REND-15 coherence question — J-E1 signal).**
The path performers ordering (conductors before ensembles, REND-15) is inverted relative to the
tag ordering (soloists first, then ensembles, then conductors, REND-14). This is a P1 coherence
question: path and tag are renderings of the same attribution model, but they order the spine
differently. The path puts the conductor first (the primary identity signal for a classical recording);
the tag puts soloists first (the CE spine order). This is not necessarily wrong — the path and tag
have different ceilings and purposes — but it should be adjudicated explicitly in V1b. Surface at
J-E1 as a coherence-violation candidate.

**D-S2-2 (REND-21 latent bug — IS_CLASSICAL hardcoded).**
`IS_CLASSICAL="1"` is hardcoded in `build_track_tags` and defaults to `"1"` in `TrackTags`. This
is currently harmless because `build_track_tags` is only called for classical releases in the
pipeline. However, if `build_track_tags` is ever called for non-classical releases, the flag will
be wrong. V1b: note the latent bug; recommend making `IS_CLASSICAL` conditional on `_top_level_class`
or removing the default from the model.

**D-S2-3 (SEL-18 writer divergence — CE-continuity question).**
At work level, `"writer"` relations go to `role_buckets.writers` (own bucket). At recording level
(`build_cea_performers`), `"writer"` merges into `composers` (CE convention: "CE merges both into
composer host tag"). This asymmetry means a `"writer"` credited at the work level appears in
`CWP_WRITERS` but not in `COMPOSER`, while a `"writer"` credited at the recording level appears in
`COMPOSER`. V1b: adjudicate whether this asymmetry is intentional or accidental; document in the
styleguide.

**D-S2-4 (C-CLASS/C-INIT validate-only — no conflicts found).**
The enacted C-CLASS vocabulary and C-INIT routing are consistent with their frozen contracts. No
apparent conflict. The CE divergence in the recital case (C-INIT case 2: implementation uses album
artist when no composer is linked in MB; CE uses composer-first even for recitals when a composer
can be inferred) is documented in the `_classical_top_dir` docstring and is a known, deliberate
divergence. This is not a conflict with the C-INIT contract (which was frozen with this behaviour);
it is evidence for V1b's adjudication of the recital-path question.

**D-S2-5 (SEL-11 concerto gate — coherence violation in miniature).**
The concerto-soloist path injection (`_tags.py:1189`) is gated strictly on `top_work.type ==
"Concerto"`. The docstring acknowledges this is the "ONLY mechanical case in scope" and defers
other canonical-soloist work types. This is a coherence violation in miniature (SEL-11): the
principle (soloist is part of canonical identity when the work is *for* that soloist) applies
beyond Concerto, but the implementation only acts on Concerto. The gate is deliberate (not
accidental), but the scope limitation is a known deferral. Surface at J-E1 as evidence for SEL-11
adjudication.

**D-S2-6 (REND-1 enacted grammar — composer not in ARTIST).**
The implementation resolves REND-1 (composer in `ARTIST`) as: `ARTIST` = MB recording credit
verbatim, no composer. The composer is in `COMPOSER` (a separate tag). `CEA_RECORDING_ARTIST` is
the CE-processed composite (soloists + ensembles + conductors). This is a de-facto ruling that
diverges from some house styles (which lead `ARTIST` with the composer). V1b: adjudicate REND-1
using this evidence plus CE evidence (D-S1-2: CE uses performer-first with composer as fallback).

**D-S2-7 (large mint volume — J-E1 signal).**
24 new cases minted. Combined with S1's 35 cases, the total mint is 59 cases beyond the 14 E0 seed
cases. The REND- layer is now at 26 cases (REND-1..26), which is large relative to the E0 seed.
Surface at J-E1 as a volume signal (D-1 in PLAN.md). The REND- inflation is expected given the
implementation's richness, but V1b should assess whether the layer is over-populated.

**D-S2-8 (ONT-9 `"bass"` ambiguity — known CE issue).**
The vocal-keyword list includes `"bass"`, which matches both bass voice and bass instrument. A bass
guitarist or double-bass player would be misclassified as a vocalist. This is a known CE issue
(the CE vocal-keyword list has the same ambiguity). V1b: note the ambiguity; recommend a more
precise keyword (e.g. `"bass voice"` or `"bass-baritone"`) or a post-classification correction.

**D-S2-9 (NORM-8 overlapping period ranges — CE convention).**
The period map has overlapping year ranges: Early Romantic (1800–1850) overlaps Classical (1750–1820)
and Late Romantic (1850–1910). First-match wins in `period_for_year`, so a work from 1810 is
classified as `"Classical"` (not `"Early Romantic"`) because `"Classical"` appears earlier in the
list. This is a CE convention (the map is a direct CE port), but the ordering dependency is not
documented. V1b: note the ordering dependency; ratify as CE convention.
