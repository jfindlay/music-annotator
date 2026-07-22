# census-ce.md — Classical Extras Editorial-Fork Inventory (S1)

**Sub-track:** V1a (source mining — styleguide arc)
**Session:** S1 — Mine CE documentation into the editorial-fork inventory
**Source:** Classical Extras v2.0.11 plugin documentation and `const.py` defaults
  (picard-plugins 2.0 branch, https://github.com/MetaBrainz/picard-plugins/tree/2.0/plugins/classical_extras)

---

## Coverage KAT

**Completeness claim:** Every configuration option surfaced in the Classical Extras options UI
(five tabs: Artists, Works and parts, Genres etc., Tag mapping, Advanced) appears as exactly one
classified fork row in the tables below, and every row carries (layer, case-ID, CE default, evidence
citation). Options that are purely mechanical/operational (cache, retry count, logging flags, Muso
database path) are grouped under a single row each where they share a layer classification.

**Basis:** `const.py` `ARTISTS_OPTIONS`, `WORKPARTS_OPTIONS`, `GENRE_OPTIONS`, `TAG_OPTIONS`,
`TAG_DETAIL_OPTIONS`, `OTHER_OPTIONS`, and the Readme.md tab-by-tab documentation (v2.0.11).

**Honest gaps:** The Advanced tab "Synonyms" and "Replacements" text fields are user-configurable
free-text; their defaults are recorded but the editorial space they open is unbounded. The Tag
mapping section (16 configurable source→tag lines) is recorded at the level of its default mapping
table; individual user overrides are out of scope for a CE-documentation census.

---

## Layer key (from STYLEGUIDE.md)

| # | Layer | Abbreviation |
|---|-------|-------------|
| 1 | Ontology | ONT- |
| 2 | Selection | SEL- |
| 3 | Normalisation | NORM- |
| 4 | Rendering | REND- |
| 5 | Epistemic register | EPIST- |

E0 case register (existing): SEL-1..11, NORM-1..2, REND-1.
All minted cases below are new; they live in this census only until V1b absorbs them.

---

## Part 1 — Artists Tab Options

### Section 1 — Enable/disable

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `classical_extra_artists` | "Create extra artist metadata" | SEL- | SEL-1 (maps: artist attribution exists) | `True` | Readme "Artists tab §1"; const.py ARTISTS_OPTIONS |

*Note: this is a master enable/disable, not itself an editorial fork. Mapped to SEL- because its
absence suppresses all performer attribution. Not a new case; the fork is whether to run performer
attribution at all.*

### Section 2 — Alias and credited-as name policy

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cea_aliases` (replace) | "Replace MB standard names by aliases — all artists" | NORM- | NORM-3 (minted) | `True` | Readme "Artists tab §2"; const.py `cea_aliases` default=True |
| `cea_aliases_composer` (composer only) | "Replace MB standard names by aliases — work-artists only" | NORM- | NORM-3 | `False` | const.py `cea_aliases_composer` default=False |
| `cea_no_aliases` | "Do not replace MB standard names by aliases" | NORM- | NORM-3 | `False` | const.py `cea_no_aliases` default=False |
| `cea_alias_overrides` | "Alias over-rides credited-as" | NORM- | NORM-4 (minted) | `True` | const.py `cea_alias_overrides` default=True |
| `cea_credited_overrides` | "Credited-as over-rides alias" | NORM- | NORM-4 | `False` | const.py `cea_credited_overrides` default=False |
| `cea_cyrillic` | "Replace non-Latin script names (Cyrillic → Latin)" | NORM- | NORM-2 (maps: native script policy) | `True` | Readme "Artists tab §2 bottom box (b)"; const.py default=True |

**NORM-3 (minted) — Alias vs. MB-standard name-form for performers and work-artists.**
CE presents three mutually exclusive choices: replace all, replace work-artists only, or no
replacement. Default: replace all. Evidence: const.py ARTISTS_OPTIONS alias group.

**NORM-4 (minted) — Alias vs. credited-as precedence.**
When both an alias and a "credited as" name exist for the same artist, CE offers a choice of which
takes precedence. Default: alias over-rides. Evidence: const.py `cea_alias_overrides`.

### Section 2 — Credited-as name scope (which contexts are honoured)

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cea_credited` | Use release credited-as | NORM- | NORM-3 | `True` | const.py |
| `cea_release_relationship_credited` | Use release-relationship credited-as | NORM- | NORM-3 | `True` | const.py |
| `cea_group_credited` | Use release-group credited-as | NORM- | NORM-3 | `True` | const.py |
| `cea_recording_credited` | Use recording credited-as | NORM- | NORM-3 | `False` | const.py (default False — notable divergence from other contexts) |
| `cea_recording_relationship_credited` | Use recording-relationship credited-as | NORM- | NORM-3 | `True` | const.py |
| `cea_track_credited` | Use track credited-as | NORM- | NORM-3 | `True` | const.py |
| `cea_performer_credited` | Use credited-as for performer | NORM- | NORM-3 | `True` | const.py |
| `cea_composer_credited` | Use credited-as for composer | NORM- | NORM-3 | `False` | const.py (default False — CE treats composer names more conservatively) |
| `cea_inst_credit` | Use credited instrument name | NORM- | NORM-5 (minted) | `True` | const.py `cea_inst_credit` |

**NORM-5 (minted) — Instrument name form: MB-standard vs. as-credited.**
CE allows instrument names in performer relationships to be rendered as the MB-standard name, the
as-credited name, or both. Default: use credited name. Evidence: const.py `cea_inst_credit`;
Readme "Genres etc. §2 Instruments".

### Section 3 — Recording artist handling

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cea_ra_use` | "Use recording artist" (put in hidden vars at minimum) | SEL- | SEL-12 (minted) | `False` | const.py `cea_ra_use` default=False |
| `cea_ra_trackartist` | Recording artist name style: track artist convention | NORM- | NORM-3 | `False` | const.py |
| `cea_ra_performer` | Recording artist name style: performer convention | NORM- | NORM-3 | `True` | const.py |
| `cea_ra_replace_ta` | Recording artist replaces track artist | SEL- | SEL-12 | `False` | const.py `cea_ra_replace_ta` |
| `cea_ra_merge_ta` | Recording artist merged with track artist | SEL- | SEL-12 | `True` | const.py `cea_ra_merge_ta` default=True |
| `cea_ra_noblank_ta` | Disallow blank recording artist | SEL- | SEL-12 | `False` | const.py |

**SEL-12 (minted) — Recording artist vs. track artist in classical context.**
In MB classical style, the track artist is the composer; the recording artist is the performer.
CE offers: keep track artist only (default when `cea_ra_use=False`), replace track artist with
recording artist, or merge both. The merge default (`cea_ra_merge_ta=True`) means `artist` tag
carries both composer and performers when enabled. Evidence: Readme "Artists tab §3"; const.py.

### Section 4 — Other artist options

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cea_arrangers` | "Modify host tags and include annotations" (gather arranger-type info) | SEL- | SEL-8 (maps: completers/orchestrators) | `True` | const.py `cea_arrangers` default=True; Readme "Artists tab §4" |
| `cea_composer_album` | "Name album as 'Composer Last Name(s): Album Name'" | REND- | REND-2 (minted) | `True` | const.py `cea_composer_album` default=True; Readme "Artists tab §4" |
| `cea_no_lyricists` | "Do not write lyricist tag if no vocal performers" | SEL- | SEL-13 (minted) | `True` | const.py `cea_no_lyricists` default=True |
| `cea_no_solo` | "Do not include attributes (solo/guest/additional) in instrument type" | ONT- | ONT-1 (minted) | `True` | const.py `cea_no_solo` default=True; Readme "Artists tab §4" |

**REND-2 (minted) — Composer-last-name prefix on album title.**
CE defaults to prepending composer last name(s) to the album name when composers are album artists,
ordered by descending duration of their music on the release. CE notes this diverges from MB style
(which excludes composer name unless it is part of the album title). Default: checked (True).
Evidence: Readme "Artists tab §4"; const.py `cea_composer_album`.

**SEL-13 (minted) — Lyricist suppression when no vocal performers.**
CE suppresses the lyricist tag (and related hidden variables) when no vocal performers are present.
Default: True. Evidence: const.py `cea_no_lyricists`.

**ONT-1 (minted) — Instrument attribute inclusion (solo/guest/additional).**
CE treats "solo", "guest", "additional" as MB-permitted instrument attributes that are editorial
in classical context. Default: exclude (True = exclude). Evidence: const.py `cea_no_solo`;
Readme "Artists tab §4".

### Section 4 — Annotation text for host tags

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cea_chorusmaster` | Annotation text for chorus master | REND- | REND-3 (minted) | `'choirmaster'` | const.py |
| `cea_orchestrator` | Annotation text for orchestrator | REND- | REND-3 | `'orch.'` | const.py |
| `cea_concertmaster` | Annotation text for concertmaster | REND- | REND-3 | `'leader'` | const.py |
| `cea_lyricist` | Annotation text for lyricist | REND- | REND-3 | `'lyrics'` | const.py |
| `cea_librettist` | Annotation text for librettist | REND- | REND-3 | `'libretto'` | const.py |
| `cea_writer` | Annotation text for writer | REND- | REND-3 | `'writer'` | const.py |
| `cea_arranger` | Annotation text for arranger | REND- | REND-3 | `'arr.'` | const.py |
| `cea_reconstructed` | Annotation text for reconstructed by | REND- | REND-3 | `'reconstructed'` | const.py |
| `cea_revised` | Annotation text for revised by | REND- | REND-3 | `'revised'` | const.py |
| `cea_translator` | Annotation text for translator | REND- | REND-3 | `'trans.'` | const.py |

**REND-3 (minted) — Role-annotation text within host tags.**
CE annotates the role type in brackets within the host tag (e.g. arranger tag contains
"Name (orch.)"). The annotation strings are configurable. Defaults above. Evidence: const.py
annotation text options; Readme "Artists tab §4 Annotations".

### Section 5 — Lyrics splitting

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cea_split_lyrics` | "Split lyrics tag" | REND- | REND-4 (minted) | `True` | const.py `cea_split_lyrics` default=True |
| `cea_lyrics_tag` | Incoming lyrics tag name | REND- | REND-4 | `'lyrics'` | const.py |
| `cea_album_lyrics` | Tag for album notes (common text) | REND- | REND-4 | `'albumnotes'` | const.py |
| `cea_track_lyrics` | Tag for track notes (unique text) | REND- | REND-4 | `'tracknotes'` | const.py |

**REND-4 (minted) — Lyrics/notes splitting into album-common vs. track-unique.**
CE splits a lyrics tag into text common to all tracks (album notes) and text unique to each track.
Default: enabled. Evidence: const.py `cea_split_lyrics`; Readme "Artists tab §5".

---

## Part 2 — Works and Parts Tab Options

### Section 1 — Enable/cache/collections

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `classical_work_parts` | "Include all work levels" (master enable) | ONT- | ONT-2 (minted) | `True` | const.py `classical_work_parts` |
| `cwp_collections` | "Include collection relationships" | ONT- | ONT-2 | `True` | const.py `cwp_collections` default=True; Readme "Works §1" |
| `use_cache` | "Use cache (if available)" | EPIST- | EPIST-1 (minted) | `True` | const.py OTHER_OPTIONS `use_cache` |

**ONT-2 (minted) — Work hierarchy scope: include "part of collection" relationships.**
CE offers the choice to include or exclude parent works linked via the "part of collection"
attribute. Default: include. This is an ontological fork: it determines which entities are treated
as part of the work's canonical identity. Evidence: const.py `cwp_collections`; Readme "Works §1".

**EPIST-1 (minted) — Cache usage for MB work lookups.**
CE caches work lookups to avoid repeated MB API calls. This is an epistemic/operational option:
using cache may miss updated MB data. Default: True (always reset to True on Picard start).
Evidence: const.py `use_cache`; Readme "Works §1".

### Section 2 — Tagging style (works source)

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cwp_titles` | Works source: title text only | NORM- | NORM-6 (minted) | `False` | const.py `cwp_titles` |
| `cwp_works` | Works source: canonical works only | NORM- | NORM-6 | `False` | const.py `cwp_works` |
| `cwp_extended` | Works source: canonical enhanced with title text | NORM- | NORM-6 | `True` | const.py `cwp_extended` default=True |
| `cwp_hierarchical_works` | Canonical work text source: full MB hierarchy | NORM- | NORM-7 (minted) | `True` | const.py `cwp_hierarchical_works` default=True |
| `cwp_level0_works` | Canonical work text source: consistent with level-0 | NORM- | NORM-7 | `False` | const.py `cwp_level0_works` |
| `cwp_derive_works_from_title` | Attempt to derive works from title if no work relationships | NORM- | NORM-6 | `True` | const.py `cwp_derive_works_from_title` default=True |

**NORM-6 (minted) — Work name source: title text, canonical MB, or enhanced canonical.**
CE offers three mutually exclusive sources for work names: (a) title text only, (b) canonical MB
work names only, (c) canonical MB enhanced with title text in `{}`. Default: (c) extended.
Evidence: const.py `cwp_extended`; Readme "Works §2 Works source".

**NORM-7 (minted) — Canonical work text resolution: full hierarchy vs. level-0 consistent.**
When using canonical or extended style, CE offers two sub-options: use each level's own MB name
(full hierarchy), or derive all levels from the level-0 work name. Default: full hierarchy.
Evidence: const.py `cwp_hierarchical_works`; Readme "Works §2 Source of canonical work text".

### Section 3 — Work name aliases

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cwp_aliases` | "Replace work names by aliases" | NORM- | NORM-2 (maps: native language/script) | `True` | const.py OTHER_OPTIONS `cwp_aliases` default=True |
| `cwp_no_aliases` | Do not replace work names by aliases | NORM- | NORM-2 | `False` | const.py |
| `cwp_aliases_all` | Alias replacement type: all works | NORM- | NORM-2 | `False` | const.py `cwp_aliases_all` |
| `cwp_aliases_greek` | Alias replacement type: non-Latin script only | NORM- | NORM-2 | `True` | const.py `cwp_aliases_greek` default=True |
| `cwp_aliases_tagged` | Alias replacement type: folksonomy-tagged works only | NORM- | NORM-2 | `False` | const.py `cwp_aliases_tagged` |
| `cwp_aliases_tag_text` | Folksonomy tag text to trigger alias | NORM- | NORM-2 | `'use_alias'` | const.py |
| `cwp_aliases_tags_all` | Look in all folksonomy tags | NORM- | NORM-2 | `True` | const.py |
| `cwp_aliases_tags_user` | Look in user's own folksonomy tags only | NORM- | NORM-2 | `False` | const.py |

### Section 4 — Tags to create (work and movement tag names)

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cwp_work_tag_multi` | Tags for Work (2-level capability) | REND- | REND-5 (minted) | `'groupheading, work'` | const.py |
| `cwp_work_tag_single` | Tags for Work (1-level capability) | REND- | REND-5 | `''` (blank) | const.py |
| `cwp_top_tag` | Tags for top-level (canonical) work | REND- | REND-5 | `'top_work, style, grouping'` | const.py |
| `cwp_movt_tag_inc` | Movement tags including embedded numbers | REND- | REND-5 | `'part, movement, subtitle'` | const.py |
| `cwp_movt_tag_exc` | Movement tags excluding embedded numbers | REND- | REND-5 | `''` (blank) | const.py |
| `cwp_movt_tag_inc1` | 1-level movement tag including numbers | REND- | REND-5 | `'movement'` | const.py |
| `cwp_movt_tag_exc1` | 1-level movement tag excluding numbers | REND- | REND-5 | `''` (blank) | const.py |
| `cwp_movt_no_tag` | Movement number tag | REND- | REND-5 | `'movementnumber'` | const.py |
| `cwp_movt_tot_tag` | Movement total tag | REND- | REND-5 | `'movementtotal'` | const.py |
| `cwp_multi_work_sep` | Multi-level work separator | REND- | REND-6 (minted) | `':'` | const.py |
| `cwp_single_work_sep` | Single-level work separator | REND- | REND-6 | `':'` | const.py |
| `cwp_movt_no_sep` | Movement number separator | REND- | REND-6 | `'.'` | const.py |

**REND-5 (minted) — Work/movement tag name assignment.**
CE allows the user to specify which tag names receive work, movement, and movement-number data.
Defaults above. This is a rendering fork: the same model data can be projected into different tag
names for different player/library software. Evidence: const.py WORKPARTS_OPTIONS tag name fields.

**REND-6 (minted) — Work hierarchy and movement-number separators.**
CE uses `:` as the default separator between work levels in multi-level work tags, and `.` as the
separator after movement numbers. Both are configurable. Evidence: const.py `cwp_multi_work_sep`,
`cwp_movt_no_sep`.

### Section 5 — Partial recordings, arrangements, medleys

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cwp_partial` | "Show partial recordings" | ONT- | ONT-3 (minted) | `True` | const.py `cwp_partial` default=True |
| `cwp_partial_text` | Text label for partial recordings | REND- | REND-7 (minted) | `'(part)'` | const.py |
| `cwp_arrangements` | "Include arrangement of" (treat arranged-from work as pseudo-parent) | ONT- | ONT-4 (minted) | `True` | const.py `cwp_arrangements` default=True |
| `cwp_arrangements_text` | Text label for arrangements | REND- | REND-7 | `'Arrangement:'` | const.py |
| `cwp_medley` | "List medleys" | ONT- | ONT-5 (minted) | `True` | const.py `cwp_medley` default=True |
| `cwp_medley_text` | Text label for medleys | REND- | REND-7 | `'Medley'` | const.py |

**ONT-3 (minted) — Partial recording identity.**
CE treats a "partial recording of" a work as a notional sub-part, appending a configurable label.
Default: enabled. This is ontological: it determines whether a partial recording is a distinct
entity or collapsed into the parent work. Evidence: const.py `cwp_partial`; Readme "Works §5".

**ONT-4 (minted) — Arrangement work as pseudo-parent.**
CE treats the original work of an arrangement as a pseudo-parent in the work hierarchy. Default:
enabled. Ontological: it determines whether the arrangement chain is part of the work's identity.
Evidence: const.py `cwp_arrangements`; Readme "Works §5".

**ONT-5 (minted) — Medley inclusion in work hierarchy.**
CE includes medley relationships in the work/movement structure. Default: enabled. Evidence:
const.py `cwp_medley`; Readme "Works §5".

**REND-7 (minted) — Label text for partial/arrangement/medley annotations.**
CE renders partial, arrangement, and medley relationships with configurable text labels prepended
or appended to work names. Defaults: `(part)`, `Arrangement:`, `Medley`. Evidence: const.py.

### Section 6 — SongKong compatibility

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cwp_use_sk` | Use SongKong work tags from file (no MB lookup) | EPIST- | EPIST-2 (minted) | `False` | const.py `cwp_use_sk` |
| `cwp_write_sk` | Write SongKong-compatible work tags | EPIST- | EPIST-2 | `False` | const.py `cwp_write_sk` |

**EPIST-2 (minted) — SongKong-compatible work tag interoperability.**
CE can read/write SongKong-compatible work tags to avoid MB lookups. Default: both False. This is
epistemic: it trades annotation completeness for speed, and the basis of the annotation changes
(file tags rather than live MB data). Evidence: const.py; Readme "Works §6".

---

## Part 3 — Genres etc. Tab Options

### Section 1 — Genre sources and filtering

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cwp_genre_tag` | Main genre tag name | REND- | REND-8 (minted) | `'genre'` | const.py |
| `cwp_subgenre_tag` | Sub-genre tag name | REND- | REND-8 | `'sub-genre'` | const.py |
| `cwp_genres_use_file` | Source genre from existing file tag | SEL- | SEL-14 (minted) | `True` | const.py |
| `cwp_genres_use_folks` | Source genre from folksonomy work tags | SEL- | SEL-14 | `True` | const.py |
| `cwp_genres_use_worktype` | Source genre from work-type attribute | SEL- | SEL-14 | `True` | const.py |
| `cwp_genres_infer` | Infer genre from artist metadata | SEL- | SEL-14 | `False` | const.py default=False |
| `cwp_genres_filter` | Apply allowed-genres filter | SEL- | SEL-14 | `True` | const.py |
| `cwp_genres_classical_main` | Classical main genres list | SEL- | SEL-14 | (long list — see const.py) | const.py |
| `cwp_genres_classical_sub` | Classical sub-genres list | SEL- | SEL-14 | (list — see const.py) | const.py |
| `cwp_genres_other_main` | General main genres list | SEL- | SEL-14 | (long list — see const.py) | const.py |
| `cwp_genres_other_sub` | General sub-genres list | SEL- | SEL-14 | (list — see const.py) | const.py |
| `cwp_genres_default` | Default genre if no match | SEL- | SEL-14 | `'Other'` | const.py |
| `cwp_genres_classical_all` | Make all tracks classical | SEL- | SEL-15 (minted) | `False` | const.py |
| `cwp_genres_classical_selective` | Make tracks classical only if genre matches | SEL- | SEL-15 | `True` | const.py |
| `cwp_genres_classical_exclude` | Exclude "Classical" from genre tag (but still treat as classical) | REND- | REND-8 | `False` | const.py |
| `cwp_genres_flag_text` | Classical flag value | REND- | REND-8 | `'1'` | const.py |
| `cwp_genres_flag_tag` | Classical flag tag name | REND- | REND-8 | `'is_classical'` | const.py |
| `cwp_genres_arranger_as_composer` | Treat arranger as composer for genre-setting | SEL- | SEL-8 (maps: completers/orchestrators) | `True` | const.py |

**SEL-14 (minted) — Genre source selection and filtering.**
CE draws genre candidates from up to four sources (file tag, folksonomy, work-type, artist
inference) and filters them against configurable allowed-genre lists. Default: file+folksonomy+
work-type enabled; inference disabled; filter enabled. Evidence: const.py GENRE_OPTIONS.

**SEL-15 (minted) — Classical classification scope.**
CE offers: classify all tracks as classical, or classify selectively based on genre matching.
Default: selective. Evidence: const.py `cwp_genres_classical_all/selective`.

**REND-8 (minted) — Genre and classical-flag tag names and rendering.**
CE writes genre data to configurable tag names, with a separate classical flag tag. Default tag
names: `genre`, `sub-genre`, `is_classical` (value `'1'`). Evidence: const.py GENRE_OPTIONS.

### Section 2 — Instruments and keys

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cwp_instruments_tag` | Instruments tag name | REND- | REND-9 (minted) | `'instrument'` | const.py |
| `cwp_instruments_MB_names` | Use MB instrument names | NORM- | NORM-5 (maps) | `True` | const.py |
| `cwp_instruments_credited_names` | Use credited instrument names | NORM- | NORM-5 | `True` | const.py |
| `cwp_key_tag` | Key tag name | REND- | REND-9 | `'key'` | const.py |
| `cwp_key_contingent_include` | Include key in work name: only if missing from title | REND- | REND-10 (minted) | `True` | const.py |
| `cwp_key_never_include` | Include key in work name: never | REND- | REND-10 | `False` | const.py |
| `cwp_key_include` | Include key in work name: always | REND- | REND-10 | `False` | const.py |

**REND-9 (minted) — Instrument and key tag names.**
CE writes instrument and key data to configurable tag names. Defaults: `instrument`, `key`.
Evidence: const.py GENRE_OPTIONS.

**REND-10 (minted) — Key signature inclusion in work name.**
CE offers three policies for including key signatures in work names: contingent (only if missing
from title — default), never, always. Evidence: const.py `cwp_key_*_include`.

### Section 3 — Work dates and periods

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cwp_workdate_tag` | Work date tag name | REND- | REND-11 (minted) | `'work_year'` | const.py |
| `cwp_workdate_source_composed` | Use composed date | SEL- | SEL-16 (minted) | `True` | const.py |
| `cwp_workdate_source_published` | Use published date | SEL- | SEL-16 | `True` | const.py |
| `cwp_workdate_source_premiered` | Use premiered date | SEL- | SEL-16 | `True` | const.py |
| `cwp_workdate_use_first` | Use work date sources sequentially (first wins) | SEL- | SEL-16 | `False` | const.py |
| `cwp_workdate_use_all` | Use all work date sources | SEL- | SEL-16 | `True` | const.py |
| `cwp_workdate_annotate` | Annotate dates (label composed/published/premiered) | REND- | REND-11 | `True` | const.py |
| `cwp_workdate_include` | Include work date in work name | REND- | REND-11 | `True` | const.py |
| `cwp_period_tag` | Period tag name | REND- | REND-11 | `'period'` | const.py |
| `cwp_period_map` | Period map (name, start, end tuples) | ONT- | ONT-6 (minted) | (Early..Contemporary — see const.py) | const.py |
| `cwp_periods_arranger_as_composer` | Treat arranger as composer for period-setting | SEL- | SEL-8 (maps) | `False` | const.py |

**SEL-16 (minted) — Work date source selection.**
CE draws work dates from composed, published, and/or premiered dates. Default: all three enabled,
all shown (not sequential). Evidence: const.py `cwp_workdate_source_*`.

**REND-11 (minted) — Work date and period rendering.**
CE writes work dates to a configurable tag, optionally annotated with source type, and optionally
embedded in work names. Period classification uses a configurable period map. Default tag names:
`work_year`, `period`. Evidence: const.py GENRE_OPTIONS date/period fields.

**ONT-6 (minted) — Classical period taxonomy.**
CE uses a configurable period map to classify works into named periods by date range. Default map:
Early (-3000–800), Medieval (800–1400), Renaissance (1400–1600), Baroque (1600–1750), Classical
(1750–1820), Early Romantic (1800–1850), Late Romantic (1850–1910), 20th Century (1910–1975),
Contemporary (1975–2525). This is ontological: it defines the period taxonomy applied to works.
Evidence: const.py `cwp_period_map`.

### Muso-specific options (Genres tab)

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cwp_use_muso_refdb` | Use Muso reference database | EPIST- | EPIST-3 (minted) | `False` | const.py |
| `cwp_muso_genres` | Use Muso classical genres list | SEL- | SEL-14 | `False` | const.py |
| `cwp_muso_classical` | Use Muso composer list to determine if classical | SEL- | SEL-15 | `False` | const.py |
| `cwp_muso_dates` | Use Muso composer dates for period | SEL- | SEL-16 | `False` | const.py |
| `cwp_muso_periods` | Use Muso period map | ONT- | ONT-6 | `False` | const.py |
| `cwp_muso_path` | Path to Muso database | EPIST- | EPIST-3 | `'C:\Users\Public\Music\muso\database'` | const.py |
| `cwp_muso_refdb` | Muso reference database filename | EPIST- | EPIST-3 | `'Reference.xml'` | const.py |

**EPIST-3 (minted) — External reference database (Muso) for genre/period/composer data.**
CE can use Muso's reference database as an alternative source for classical genre lists, composer
rosters, and period maps. Default: disabled. Evidence: const.py OTHER_OPTIONS Muso group.

---

## Part 4 — Tag Mapping Tab Options

### Section 1 — Initial tag processing

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cea_blank_tag` | Tags to blank before mapping | REND- | REND-12 (minted) | `'artist, artistsort'` | const.py TAG_OPTIONS |
| `cea_blank_tag_2` | Tags to blank (group 2) | REND- | REND-12 | `'performer:orchestra, performer:choir, performer:choir vocals'` | const.py |
| `cea_keep` | File tags to keep (not overwrite) | REND- | REND-12 | `''` (blank) | const.py |
| `cea_clear_tags` | Clear previous tags | REND- | REND-12 | `False` | const.py |
| `cea_tag_sort` | Populate sort tags | REND- | REND-12 | `True` | const.py |

**REND-12 (minted) — Tag blanking and sort-tag population policy.**
CE blanks specified tags before applying mappings, and optionally populates sort-name variants.
Default: blank `artist, artistsort` and performer sub-tags; populate sort tags. Evidence: const.py
TAG_OPTIONS.

### Section 2 — Tag mapping detail lines (default mapping table)

The default 16-line tag mapping table defines CE's default output grammar. The first 9 lines have
non-empty defaults (from const.py `default_list`):

| Line | Source(s) | Target tag(s) | Conditional | Layer | Case-ID | Evidence |
|---|---|---|---|---|---|---|
| 1 | `album_soloists, album_ensembles, album_conductors` | `artist, artists` | No | REND- | REND-1 (maps) | const.py default_list[0] |
| 2 | `recording_artists` | `artist, artists` | Yes | REND- | REND-1 | const.py default_list[1] |
| 3 | `soloist_names, ensemble_names, conductors` | `artist, artists` | Yes | REND- | REND-1 | const.py default_list[2] |
| 4 | `soloists` | `soloists, trackartist, involved people` | No | REND- | REND-13 (minted) | const.py default_list[3] |
| 5 | `release` | `release_name` | No | REND- | REND-13 | const.py default_list[4] |
| 6 | `ensemble_names` | `band` | No | REND- | REND-13 | const.py default_list[5] |
| 7 | `composers` | `artist` | Yes | REND- | REND-1 | const.py default_list[6] |
| 8 | `MB_artists` | `composer` | Yes | REND- | REND-1 | const.py default_list[7] |
| 9 | `arranger` | `composer` | Yes | REND- | REND-1 | const.py default_list[8] |
| 10–16 | (blank) | (blank) | — | — | — | const.py TAG_DETAIL_OPTIONS |

**REND-1 (maps — existing E0 case) — Composer in `ARTIST`.**
CE's default tag mapping (lines 1–3, 7–9) constructs `artist` from a cascade: album soloists +
ensembles + conductors first, then recording artists (conditional), then soloist+ensemble+conductor
names (conditional), then composers (conditional), then MB artists as composer (conditional), then
arranger as composer (conditional). This is the CE evidence for REND-1: CE's default `ARTIST`
grammar leads with performers (soloists/ensembles/conductors) and falls back to composer. The
cascade is conditional so that earlier non-empty values prevent later ones from appending.
Evidence: const.py `default_list`; Readme "Tag mapping tab §2".

**REND-13 (minted) — Performer sub-tag grammar (soloists, band, involved people).**
CE's default mapping writes `soloists` (with instruments) to `soloists, trackartist, involved
people`, and `ensemble_names` to `band`. This defines CE's secondary performer rendering surface
beyond `artist`. Evidence: const.py `default_list` lines 4, 6.

---

## Part 5 — Advanced Tab Options

### Section 1 — General

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `ce_no_run` | "Do not run CE for tracks where no pre-existing file detected" | EPIST- | EPIST-4 (minted) | `False` | const.py `ce_no_run`; Readme "Advanced §1" |

**EPIST-4 (minted) — Conditional processing: skip tracks without pre-existing files.**
CE can skip processing for tracks with no matched file. Default: False (always run). Evidence:
const.py `ce_no_run`.

### Section 2 — Ensemble identification strings

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cea_orchestras` | Orchestra identification strings | ONT- | ONT-7 (minted) | `'orchestra, philharmonic, ...'` | const.py |
| `cea_choirs` | Choir identification strings | ONT- | ONT-7 | `'choir, chorus, singers, ...'` | const.py |
| `cea_groups` | Group/ensemble identification strings | ONT- | ONT-7 | `'ensemble, band, group, trio, ...'` | const.py |

**ONT-7 (minted) — Ensemble type classification by name substring.**
CE classifies performers as orchestras, choirs, or groups by matching name substrings against
configurable lists. This is ontological: it determines which role category a performer belongs to.
Default strings: see const.py `cea_orchestras`, `cea_choirs`, `cea_groups`. Evidence: const.py;
Readme "Advanced §2".

### Section 3 — Works and parts algorithm parameters

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `cwp_retries` | Max retries for MB work lookups | EPIST- | EPIST-1 | `6` | const.py |
| `cwp_allow_empty_parts` | Allow blank part names for arrangements/partials | REND- | REND-7 | `True` | const.py `cwp_allow_empty_parts` |
| `cwp_common_chars` | Min common words to eliminate (parent-child stripping) | NORM- | NORM-7 | `2` | const.py |
| `cwp_proximity` | In-string proximity trigger for title extension | NORM- | NORM-6 | `2` | const.py |
| `cwp_end_proximity` | End-string proximity trigger | NORM- | NORM-6 | `1` | const.py |
| `cwp_split_hyphenated` | Treat hyphenated words as two words for comparison | NORM- | NORM-6 | `True` | const.py |
| `cwp_substring_match` | Similarity threshold % for title/work matching | NORM- | NORM-6 | `100` | const.py |
| `cwp_fill_part` | Disallow empty part names (fill with title text) | NORM- | NORM-6 | `True` | const.py |
| `cwp_prepositions` | Prepositions/conjunctions to exclude from "new" words | NORM- | NORM-6 | (long list — see const.py) | const.py |
| `cwp_removewords` | Prefixes to ignore in work name matching | NORM- | NORM-6 | (list — see const.py) | const.py |
| `cwp_synonyms` | Synonym tuples for work name comparison | NORM- | NORM-6 | (list — see const.py) | const.py |
| `cwp_replacements` | Replacement tuples for work name text | NORM- | NORM-6 | (placeholder — see const.py) | const.py |

### Section 5 — Logging options

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `log_error` | Log errors | EPIST- | EPIST-5 (minted) | `True` | const.py |
| `log_warning` | Log warnings | EPIST- | EPIST-5 | `True` | const.py |
| `log_debug` | Log debug messages | EPIST- | EPIST-5 | `False` | const.py |
| `log_basic` | Basic session log | EPIST- | EPIST-5 | `True` | const.py |
| `log_info` | Info log | EPIST- | EPIST-5 | `False` | const.py |

**EPIST-5 (minted) — Logging verbosity.**
CE writes errors and warnings to special tags (`001_errors`, `002_important_warning`) and to a
session log. Debug and info logging are off by default. Evidence: const.py OTHER_OPTIONS log group.

### Section 6 — Special tags and option persistence

| Option key | CE label | Layer | Case-ID | CE default | Evidence citation |
|---|---|---|---|---|---|
| `ce_version_tag` | Tag for CE version stamp | EPIST- | EPIST-6 (minted) | `'stamp'` | const.py |
| `cea_options_tag` | Tag for artist options | EPIST- | EPIST-6 | `'comment'` | const.py |
| `cwp_options_tag` | Tag for work-parts options | EPIST- | EPIST-6 | `'comment'` | const.py |
| `cea_override` | Over-ride artist options from file tags | EPIST- | EPIST-6 | `False` | const.py |
| `cwp_override` | Over-ride work-parts options from file tags | EPIST- | EPIST-6 | `False` | const.py |
| `ce_tagmap_override` | Over-ride tag-map options from file tags | EPIST- | EPIST-6 | `False` | const.py |
| `ce_genres_override` | Over-ride genres options from file tags | EPIST- | EPIST-6 | `False` | const.py |
| `ce_options_overwrite` | Overwrite UI options with saved file-tag options | EPIST- | EPIST-6 | `False` | const.py |
| `ce_show_ui_tags` | Show additional tags in Picard UI | EPIST- | EPIST-6 | `False` | const.py |
| `ce_ui_tags` | Tags for UI columns (diff display) | EPIST- | EPIST-6 | (see const.py) | const.py |

**EPIST-6 (minted) — Option persistence and provenance in file tags.**
CE can save its configuration options into file tags (e.g. `comment`, `stamp`) and later restore
them. This is epistemic: it records the basis of the annotation (which options were used) alongside
the annotation itself. Default: persistence disabled. Evidence: const.py OTHER_OPTIONS override group.

---

## Part 6 — CE Tag Vocabulary and Semantics (Compatibility Floor)

This section enumerates the CE tag names and their established meanings, per the CE-continuity
posture (STYLEGUIDE standing rule 1). Extensions must not redefine these.

### Standard Picard tags used/modified by CE

| Tag name | CE meaning | Notes |
|---|---|---|
| `artist` | Composite performer/composer string (see REND-1) | CE default: album soloists+ensembles+conductors cascade |
| `artists` | Multi-valued version of `artist` | CE writes same sources as `artist` |
| `artistsort` | Sort form of `artist` | CE blanks by default then repopulates |
| `artists_sort` | Sort form of `artists` | CE populates if `cea_tag_sort=True` |
| `albumartist` | Not directly modified by CE | Determined by Picard; CE may prefix with composer last name via `cea_composer_album` |
| `composer` | Composer name(s) | CE writes MB track artist (composer) here; also `MB_artists` and `arranger` via tag mapping |
| `composersort` | Sort form of composer | CE populates |
| `conductor` | Conductor name(s) | CE writes conductors; also chorus masters (via `cea_arrangers`) |
| `arranger` | Arranger name(s) with role annotation | CE gathers all arranger-type roles with annotation text |
| `lyricist` | Lyricist name(s) | CE writes lyricists; also translators |
| `performer:*` | Performer with instrument sub-key | CE writes soloists with instrument; also `performer:orchestra`, `performer:choir` |
| `work` | Level-0 work name | Standard Picard tag; CE populates from MB work hierarchy |
| `movement` | Movement/part name | Standard Picard tag; CE default: `cwp_movt_tag_inc = 'part, movement, subtitle'` |
| `movementnumber` | Computed movement sequence number | Standard Picard tag; CE default: `cwp_movt_no_tag = 'movementnumber'` |
| `movementtotal` | Total movements in parent work | Standard Picard tag; CE default: `cwp_movt_tot_tag = 'movementtotal'` |
| `genre` | Genre tag | CE default main genre tag |
| `instrument` | Instrument names | CE default instruments tag |
| `key` | Key signature(s) | CE default key tag |

### CE-specific output tags (written by default)

| Tag name | CE meaning | Notes |
|---|---|---|
| `groupheading` | Multi-level work name (2-level, `::` separator) | Default in `cwp_work_tag_multi`; Muso primary |
| `top_work` | Top-level canonical work name | Default in `cwp_top_tag` |
| `style` | Also receives top-level work name | Default in `cwp_top_tag` |
| `grouping` | Also receives top-level work name | Default in `cwp_top_tag` |
| `part` | Movement including embedded number | Default in `cwp_movt_tag_inc` |
| `subtitle` | Also receives movement | Default in `cwp_movt_tag_inc` |
| `soloists` | Soloists with instruments | Default tag mapping line 4 |
| `trackartist` | Also receives soloists | Default tag mapping line 4 |
| `involved people` | Also receives soloists | Default tag mapping line 4 |
| `band` | Ensemble names | Default tag mapping line 6 |
| `release_name` | Release title | Default tag mapping line 5 |
| `sub-genre` | Sub-genre | CE default sub-genre tag |
| `is_classical` | Classical flag (value `'1'`) | CE default classical flag tag |
| `work_year` | Work date (year or range) | CE default work date tag |
| `period` | Classical period name | CE default period tag |
| `stamp` | CE version stamp | CE default version tag |
| `comment` | CE options (artist + work-parts) | CE default options tag |
| `albumnotes` | Common lyrics/notes text | CE default album notes tag |
| `tracknotes` | Track-unique lyrics/notes text | CE default track notes tag |
| `show work movement` | iTunes flag (set to 1 if work has ≥1 level) | CE sets automatically |
| `000_major_warning` | Major warning messages | CE error tag |
| `001_errors` | Error messages | CE error tag |
| `002_important_warning` | Important warning messages | CE error tag |

### CE hidden variables (_cwp_ prefix — Works and Parts)

These are internal variables available for scripting; not written to file tags unless mapped.

| Variable | CE meaning |
|---|---|
| `_cwp_work_n` (n≥0) | MB work name at level n; `_cwp_work_0` = standard Picard `work` |
| `_cwp_work_top` | Top-level canonical work name (no annotations) |
| `_cwp_workid_n` | MB work ID at level n; `_cwp_workid_0` = `MusicBrainz Work Id` |
| `_cwp_workid_top` | Top-level work ID |
| `_cwp_part_n` | Stripped version of `_cwp_work_n` (parent text removed) |
| `_cwp_part_levels` | Number of work levels for this track |
| `_cwp_work_part_levels` | Max work levels for any track sharing the same top work |
| `_cwp_single_work_album` | Flag: 1 if only one top work on album |
| `_cwp_work` | Single-level work name (canonical source) |
| `_cwp_groupheading` | Multi-level work name (canonical source) |
| `_cwp_part` | Movement name (= `_cwp_part_0` generally) |
| `_cwp_inter_work` | Intermediate works between part and work |
| `_cwp_movt_num` | Movement sequence number |
| `_cwp_movt_tot` | Total movements in parent |
| `_cwp_X0_part_0` | Stripped level-0 work (elements repeating in level 1 removed) |
| `_cwp_X0_work_n` | Elements of level-0 work repeating within level n |
| `_cwp_title` | Track title with composer prefix removed |
| `_cwp_title_work_n` | Work name derived from title at level n |
| `_cwp_title_part_n` | Part name derived from title at level n |
| `_cwp_title_part_levels` | Part levels from title |
| `_cwp_title_work_levels` | Work levels from title |
| `_cwp_title_work` | Single-level work from title |
| `_cwp_title_groupheading` | Multi-level work from title |
| `_cwp_extended_part` | `_cwp_part` + title additions in `{}` |
| `_cwp_extended_groupheading` | `_cwp_groupheading` + title additions in `{}` |
| `_cwp_extended_work` | `_cwp_work` + title additions in `{}` |
| `_cwp_extended_inter_work` | `_cwp_inter_work` + title additions in `{}` |
| `_cwp_composers` | Composer names (from work relationships) |
| `_cwp_composers_sort` | Sort names of above |
| `_cwp_composer_lastnames` | Last names of composers |
| `_cwp_writers` | Writer names |
| `_cwp_writers_sort` | Sort names of above |
| `_cwp_arrangers` | Arranger names with instrument/voice annotation |
| `_cwp_arranger_names` | Arranger names only (no annotation) |
| `_cwp_arrangers_sort` | Sort names of arrangers |
| `_cwp_orchestrators` | Orchestrator names |
| `_cwp_orchestrators_sort` | Sort names of above |
| `_cwp_reconstructors` | "Reconstructed by" names |
| `_cwp_reconstructors_sort` | Sort names of above |
| `_cwp_revisors` | "Revised by" names |
| `_cwp_revisors_sort` | Sort names of above |
| `_cwp_lyricists` | Lyricist names |
| `_cwp_lyricists_sort` | Sort names of above |
| `_cwp_librettists` | Librettist names |
| `_cwp_librettists_sort` | Sort names of above |
| `_cwp_translators` | Translator names |
| `_cwp_translators_sort` | Sort names of above |
| `_cwp_keys` | Key signatures from all work levels |
| `_cwp_composed_dates` | Composed date(s) |
| `_cwp_published_dates` | Published date(s) |
| `_cwp_premiered_dates` | Premiered date(s) |
| `_cwp_candidate_genres` | All genre candidates before filtering |
| `_cwp_worktype_genres` | Work type attributes from work and parents |
| `_cwp_untagged_genres` | Genres filtered out by allowed-genres list |
| `_cwp_unrostered_composers` | Composers not in Muso roster |
| `_cwp_error` | Error message |
| `_cwp_warning` | Warning message |

### CE hidden variables (_cea_ prefix — Artists)

| Variable | CE meaning |
|---|---|
| `_cea_recording_artist` | Artist credited with the recording (singular, join-phrase format) |
| `_cea_recording_artists` | Multi-valued recording artists |
| `_cea_MB_artists` | Original track artists before any replacement/merge |
| `_cea_soloists` | Performers who are not ensembles or conductors (with instruments) |
| `_cea_recording_artistsort` | Sort name of `_cea_recording_artist` |
| `_cea_recording_artists_sort` | Sort names of `_cea_recording_artists` |
| `_cea_soloist_names` | Soloist names only (no instruments) |
| `_cea_soloists_sort` | Sort names of soloists |
| `_cea_vocalists` | Soloists who are vocalists (with voice type) |
| `_cea_vocalist_names` | Vocalist names only |
| `_cea_instrumentalists` | Soloists with instruments (non-vocal) |
| `_cea_instrumentalist_names` | Instrumentalist names only |
| `_cea_other_soloists` | Soloists without specified instrument/voice |
| `_cea_ensembles` | Ensemble performers (with type in brackets) |
| `_cea_ensemble_names` | Ensemble names only |
| `_cea_ensembles_sort` | Sort names of ensembles |
| `_cea_album_soloists` | Soloists who are also album artists |
| `_cea_album_soloists_sort` | Sort names of above |
| `_cea_album_conductors` | Conductors who are also album artists |
| `_cea_album_conductors_sort` | Sort names of above |
| `_cea_album_ensembles` | Ensembles who are also album artists |
| `_cea_album_ensembles_sort` | Sort names of above |
| `_cea_album_composers` | Composers who are also album artists |
| `_cea_album_composers_sort` | Sort names of above |
| `_cea_album_track_composer_lastnames` | Last names of composers of this track who are album artists |
| `_cea_album_composer_lastnames` | Last names of composers of any track who are album artists |
| `_cea_support_performers` | Soloists who are NOT album artists |
| `_cea_support_performers_sort` | Sort names of above |
| `_cea_composers` | Composer names (incorporating naming options) |
| `_cea_composer_lastnames` | Last names of above |
| `_cea_conductors` | Conductor names (incorporating naming options) |
| `_cea_performers` | Performer names (incorporating naming options) |
| `_cea_arrangers` | Arrangers for the recording (with instrument/voice annotation) |
| `_cea_orchestrators` | Orchestrators (from Picard arranger tag) |
| `_cea_chorusmasters` | Chorus masters |
| `_cea_leaders` | Concertmasters/leaders |
| `_cea_instruments` | Instrument names (MB standard) |
| `_cea_instruments_credited` | Instrument names (as-credited) |
| `_cea_instruments_all` | Both MB and as-credited instrument names |
| `_cea_work_type` | Genre(s) inferred from artist information |
| `_cea_work_type_if_classical` | As above, only if classical |

---

## Part 7 — CE Ordering and Grammar Conventions

### Performer ordering (spine order)

CE's `ARTIST_TYPE_ORDER` in const.py defines the ordering of artist types:

```
vocal/instrument: 1 (tied — soloists)
performer: 0 (generic performer, highest priority)
performing orchestra: 2
concertmaster: 3
conductor: 4
chorus master: 5
composer: 6
writer: 7
reconstructed by: 8
instrument arranger / vocal arranger: 9 (tied)
arranger: 11
orchestrator: 12
revised by: 13
lyricist: 14
librettist: 15
translator: 16
```

This ordering is the CE-enacted spine: performers (soloists) → orchestras → conductors → composers
→ arrangers → lyricists. Note: `performer` (generic) has order 0 (highest), but `vocal`/`instrument`
have order 1 — this means a generic performer sorts before a named-instrument performer.

### Tag mapping cascade for `artist`

CE's default `artist` construction (from tag mapping default_list) is a conditional cascade:
1. `album_soloists + album_ensembles + album_conductors` → `artist, artists` (unconditional)
2. `recording_artists` → `artist, artists` (conditional: only if artist was empty)
3. `soloist_names + ensemble_names + conductors` → `artist, artists` (conditional)
4. `composers` → `artist` (conditional)
5. `MB_artists` → `composer` (conditional)
6. `arranger` → `composer` (conditional)

This is CE's enacted REND-1 stance: `artist` leads with performers; composer is a fallback.

### Work name grammar

- Work levels separated by `:` (default `cwp_multi_work_sep`)
- Movement number followed by `.` (default `cwp_movt_no_sep`)
- Extended title additions enclosed in `{}`
- Partial recording label: `(part)` prepended
- Arrangement label: `Arrangement:` prepended
- Medley label: `Medley` in brackets after work name (for "medley of") or in parent work name
- Key signature in brackets after work name: `(E minor)` — contingent by default
- Work date in brackets after work name — enabled by default
- Multi-level work tag uses `::` as level separator within a single tag value (for Muso)

### Separator for multiple values in string tags

CE uses `;` (semicolon) as the join phrase for multiple artists in string-format tags (e.g.
`artist`). Multi-valued tags use separate tag instances where the format supports it.

### Album name prefix

CE prepends `Composer Last Name(s): ` to the album name when `cea_composer_album=True` (default).
Multiple composers ordered by descending duration of their music on the release.

### Artist type → host tag mapping

From const.py `tag_strings()` and the Readme table:

| MB relationship type | CE host tag | CE hidden variable |
|---|---|---|
| writer | composer | `_cwp_writers` |
| composer | composer | `_cwp_composers` |
| lyricist | lyricist | `_cwp_lyricists` |
| librettist | lyricist | `_cwp_librettists` |
| revised by | arranger | `_cwp_revisors` |
| translator | lyricist | `_cwp_translators` |
| arranger | arranger | `_cwp_arrangers` |
| reconstructed by | arranger | `_cwp_reconstructors` |
| orchestrator | arranger | `_cwp_orchestrators` |
| instrument arranger | arranger | `_cwp_arrangers` (with instrument annotation) |
| vocal arranger | arranger | `_cwp_arrangers` (with voice annotation) |
| performer | performer: | `_cea_performers` |
| instrument | performer: | `_cea_performers` |
| vocal | performer: | `_cea_performers` |
| performing orchestra | performer:orchestra | `_cea_ensembles` |
| conductor | conductor | `_cea_conductors` |
| chorus master | conductor | `_cea_chorusmasters` |
| concertmaster | performer (with annotation) | `_cea_leaders` |

---

## Part 8 — CE Conventions the Implementation May Have Diverged From

These are observations for V1b to adjudicate; no ruling is made here.

1. **Album name prefix (`cea_composer_album=True`):** CE defaults to prepending composer last names
   to the album name. The implementation (`_tags.py`) uses `build_dest_path` which constructs a
   directory path; whether it applies the same composer-prefix convention to the album tag is a
   potential divergence site. Flag for S2 to verify.

2. **`artist` tag cascade:** CE's default mapping constructs `artist` from a conditional cascade
   ending with `composers` and `MB_artists → composer`. The implementation's `ARTIST` tag grammar
   (noted in PLAN.md as `_pipeline.py:1742` "MB recording credit verbatim") may differ from CE's
   cascade. Flag for S2 to verify against REND-1.

3. **Performer ordering:** CE's `ARTIST_TYPE_ORDER` places generic `performer` at order 0 (before
   soloists at order 1). The implementation's `build_cea_performers` function may use a different
   ordering. Flag for S2.

4. **Separator conventions:** CE uses `:` for work-level separation and `.` for movement numbers.
   The implementation's separator choices should be verified against these defaults in S2.

5. **Partial recording label:** CE default is `(part)`. The implementation may use a different
   label or no label. Flag for S2.

6. **Arrangement handling:** CE treats the original work of an arrangement as a pseudo-parent
   (default: enabled). The implementation's handling of arrangement relationships should be
   verified in S2.

7. **Lyricist suppression:** CE suppresses lyricist tag when no vocal performers (`cea_no_lyricists
   =True`). The implementation should be checked for this behaviour in S2.

8. **`is_classical` flag:** CE writes `is_classical = '1'` by default. The implementation may not
   write this tag. Flag for S2.

9. **`groupheading` tag:** CE's primary multi-level work tag is `groupheading` (with `work` as
   secondary). The implementation uses `groupheading` and `work` — alignment should be verified.

---

## Part 9 — Minted Case-ID Register (this census)

All new case-IDs minted in this census. These live here until V1b absorbs them into STYLEGUIDE.md.

| Case-ID | Layer | Name | Status |
|---|---|---|---|
| ONT-1 | Ontology | Instrument attribute inclusion (solo/guest/additional) | minted |
| ONT-2 | Ontology | Work hierarchy scope: collection relationships | minted |
| ONT-3 | Ontology | Partial recording identity | minted |
| ONT-4 | Ontology | Arrangement work as pseudo-parent | minted |
| ONT-5 | Ontology | Medley inclusion in work hierarchy | minted |
| ONT-6 | Ontology | Classical period taxonomy | minted |
| ONT-7 | Ontology | Ensemble type classification by name substring | minted |
| SEL-12 | Selection | Recording artist vs. track artist in classical context | minted |
| SEL-13 | Selection | Lyricist suppression when no vocal performers | minted |
| SEL-14 | Selection | Genre source selection and filtering | minted |
| SEL-15 | Selection | Classical classification scope | minted |
| SEL-16 | Selection | Work date source selection | minted |
| NORM-3 | Normalisation | Alias vs. MB-standard name-form for performers and work-artists | minted |
| NORM-4 | Normalisation | Alias vs. credited-as precedence | minted |
| NORM-5 | Normalisation | Instrument name form: MB-standard vs. as-credited | minted |
| NORM-6 | Normalisation | Work name source: title text, canonical MB, or enhanced canonical | minted |
| NORM-7 | Normalisation | Canonical work text resolution: full hierarchy vs. level-0 consistent | minted |
| REND-2 | Rendering | Composer-last-name prefix on album title | minted |
| REND-3 | Rendering | Role-annotation text within host tags | minted |
| REND-4 | Rendering | Lyrics/notes splitting into album-common vs. track-unique | minted |
| REND-5 | Rendering | Work/movement tag name assignment | minted |
| REND-6 | Rendering | Work hierarchy and movement-number separators | minted |
| REND-7 | Rendering | Label text for partial/arrangement/medley annotations | minted |
| REND-8 | Rendering | Genre and classical-flag tag names and rendering | minted |
| REND-9 | Rendering | Instrument and key tag names | minted |
| REND-10 | Rendering | Key signature inclusion in work name | minted |
| REND-11 | Rendering | Work date and period rendering | minted |
| REND-12 | Rendering | Tag blanking and sort-tag population policy | minted |
| REND-13 | Rendering | Performer sub-tag grammar (soloists, band, involved people) | minted |
| EPIST-1 | Epistemic | Cache usage for MB work lookups | minted |
| EPIST-2 | Epistemic | SongKong-compatible work tag interoperability | minted |
| EPIST-3 | Epistemic | External reference database (Muso) for genre/period/composer data | minted |
| EPIST-4 | Epistemic | Conditional processing: skip tracks without pre-existing files | minted |
| EPIST-5 | Epistemic | Logging verbosity | minted |
| EPIST-6 | Epistemic | Option persistence and provenance in file tags | minted |

**Mint count summary:** ONT: 7, SEL: 5 (SEL-12..16), NORM: 5 (NORM-3..7), REND: 12 (REND-2..13),
EPIST: 6 (EPIST-1..6). Total new: 35 cases.

E0 cases mapped (not minted): SEL-1..11 (various), NORM-1..2, REND-1.

---

## Part 10 — Discoveries

**D-S1-1 (J-E1 signal — large mint volume).**
35 new cases minted. This is a substantial volume relative to the 14 E0 seed cases (SEL-1..11,
NORM-1..2, REND-1). The E0 seed was deliberately sparse (open cases, not adjudicated); CE's
option space fills in the missing coverage. The mint volume is expected given CE's breadth, but
V1b / J-E1 should assess whether the REND- and EPIST- layers are over-populated relative to their
design intent. Specifically: REND has 13 cases (REND-1..13) and EPIST has 6 — both layers were
empty in E0. This is not a schema-fit failure (all cases fit their layers cleanly) but the volume
is a signal to surface.

**D-S1-2 (REND-1 evidence — CE's enacted `ARTIST` grammar).**
CE's default tag mapping constructs `artist` from a conditional cascade: album performers first,
then recording artists, then track-level performers, then composers as fallback. This is the CE
evidence for REND-1 (composer in `ARTIST`): CE's default does NOT lead with the composer — it
leads with performers and uses composer only as a conditional fallback. This is a significant
finding for V1b's REND-1 adjudication.

**D-S1-3 (SEL-12 — recording artist vs. track artist: CE's default is merge, not replace).**
CE defaults `cea_ra_merge_ta=True` and `cea_ra_use=False`. When `cea_ra_use` is enabled, the
default action is to merge recording artists with track artists (not replace). This means the
`artist` tag can carry both composer and performers simultaneously. This is the CE evidence for
SEL-12 and also bears on REND-1.

**D-S1-4 (NORM-3 — recording credited-as is off by default).**
CE defaults `cea_recording_credited=False` while all other credited-as contexts default to True.
This asymmetry (recording context treated more conservatively than release/track contexts) is a
notable CE convention. Evidence: const.py.

**D-S1-5 (NORM-3 — composer credited-as is off by default).**
CE defaults `cea_composer_credited=False`. Composer names are treated more conservatively than
performer names for credited-as substitution. This is a CE stance on composer name stability.

**D-S1-6 (ONT-7 — ensemble classification by name substring is the CE mechanism).**
CE classifies performers as orchestras/choirs/groups by substring matching against configurable
lists. This is the CE mechanism for the soloists-vs-ensembles distinction (SEL-2, SEL-4, SEL-5
territory). The lists are editorial: `cea_orchestras` default includes "philharmonic",
"philharmoniker", "academy", "symphony", "orkester". V1b should note this as the CE evidence for
the ensemble-identification question.

**D-S1-7 (potential schema-fit question — EPIST- layer).**
Several EPIST- cases (EPIST-1 cache, EPIST-4 no-run, EPIST-5 logging) are operational/performance
options rather than epistemic claims about annotation confidence or provenance. They were placed in
EPIST- as the closest layer (they affect what data is available and how it is sourced), but they
may not fit the STYLEGUIDE's EPIST- definition (confidence, provenance, contestation marking)
cleanly. Surface at J-E1 as a potential schema-fit question (D-4 in PLAN.md). Not a blocking
issue — the cases are recorded; the layer assignment is the question.

**D-S1-8 (CE-continuity — `is_classical` flag).**
CE writes `is_classical = '1'` by default. This is a CE-established tag name and value. Any
implementation that writes a classical flag must use this name and value (or a new name — not a
redefinition). The implementation should be checked in S2.

**D-S1-9 (CE-continuity — `groupheading` with `::` separator).**
CE uses `groupheading` as the primary multi-level work tag, with `::` as the intra-tag level
separator. This is a CE-established convention. The implementation uses `groupheading` — alignment
of the separator convention should be verified in S2.

**D-S1-10 (CE-continuity — `stamp` and `comment` for option provenance).**
CE writes its version and options to `stamp` and `comment` tags. The implementation's provenance
mechanism (sidecar files, per STYLEGUIDE 5.5) is different from CE's in-tag approach. This is a
known divergence (library-level vs. MB-derivable partition) — not a conflict, but worth noting
for V1b's EPIST-6 adjudication.
