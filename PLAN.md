# music-annotator — Development Plan

This document captures the agreed implementation plan, open design questions, and deferred items
as of the current session.  It is intended as a durable reference for resuming work.

---

## Recently completed (this session)

- **Primary work selection** (`_works.py`, `_pipeline.py`): `select_primary_performance_work()`
  scores each performance-linked work's root by work type (+2) and absence of `based-on/backward`
  (+1), selecting the highest-scoring candidate.  Handles the Beethoven concerto vs. Kreisler
  cadenza case correctly.

- **Additional composer filtering** (`models.py`, `_works.py`, `_tags.py`): `RoleBuckets`
  gains `additional_composers`.  MB `"additional"` / `"assistant"` composer attributes route to
  this bucket instead of `composers`, keeping subsidiary completion credits (e.g. Süssmayr on the
  Mozart Requiem) out of the primary composer field and directory name.

- **Cover art at original resolution** (`_mb_api.py`): removed `size="500"` from both
  `mb.get_image` and `mb.get_release_group_image_front`.

- **Post-copy confirmation message** (`_pipeline.py`): after all files pass `_verify_copy`, a
  `Verified OK: N file(s) written and confirmed to: …` message is printed before the source-
  delete prompt.  Provenance invariant documented in `AGENTS.md`.

- **User prompt consistency** (`_pipeline.py`, `_discover.py`): unified colour scheme —
  `[bold cyan]>` input prompt, `[bold red]` warnings, `[bold green]` success, `[bold yellow]`
  advisory — across collision, verified-OK, candidate-selection, and delete-confirmation prompts.

- **`direct_work` fallback clarified** (`_tags.py`): comment and empty-id guard added; the
  break-on-first-performance pattern is explicitly documented as a last-resort label only.

---

## Completed — directory and filename restructuring

### 1. Intermediate compositional-division directories

**Decision:** Introduce one subdirectory per intermediate work-hierarchy level when the hierarchy
has 3+ levels, but only for levels that are compositional subdivisions of a single work — not
collection/cycle wrappers.

**How collection wrappers are excluded:** `select_primary_performance_work` already selects the
primary work (e.g. Die Walküre, not the Ring cycle above it).  All levels in the hierarchy passed
to `build_dest_path` are therefore compositional subdivisions by construction.  No additional
filtering is needed.

**Depth in practice (verified against live MB data):**
- 2-level (unchanged): Symphony → Movement (Beethoven concerto, Tchaikovsky serenade movement)
- 3-level (new): Opera → Act → Number (Otello, Don Giovanni)
- 3-level (new): Oratorio → Part → Number (Bach St Matthew Passion)
- The Ring Cycle produces 3 levels at the filesystem, not 4: `select_primary_performance_work`
  selects Die Walküre (typed Opera), making the Ring cycle root invisible to `build_dest_path`.

**Directory naming:**
- All intermediate dirs prefixed `nn - ` (zero-padded 2 digits, directory-scoped).
- Same `nn - ` prefix on all track filenames (unchanged convention, now explicitly standardised).
- `nn` source: MB `ordering-key` on the `parts/backward` relation → `MOVEMENTNUMBER` →
  `track.position`.  Imperfect MB data is acceptable.

**`MOVEMENTNUMBER` note:** this tag remains the composer's global movement number across the whole
work (e.g. No. 39 in the Messiah) and appears in the title string of the filename.  It is NOT
the directory-local `nn` prefix.  The directory-local `nn` is a separate value derived from
`ordering-key`.

**Example layout (Verdi Otello):**
```
Wagner - Muti; Orchestra del Teatro alla Scala/
  Otello [1978]/
    01 - Preludio/
    02 - Atto I/
      01 - Esultate!.flac
      02 - Fuoco di gioia.flac
      …
    03 - Atto II/
      01 - …
```

**Example layout (Handel Messiah — where Handel numbers movements globally):**
```
Handel - Gardiner; English Baroque Soloists/
  Messiah [1982]/
    01 - Part I/
      01 - No. 1 Sinfony.flac
      02 - No. 2 Comfort ye.flac
      …
    02 - Part II/
      01 - No. 20 Behold the Lamb of God.flac
      …
```

### 2. Recording year in work directory (replacing full MBID)

**Decision:** Replace `Work Title (full-MBID-UUID)` with `Work Title [YYYY]`.

**Source:** `release_group.first_release_date` (already stored as `ORIGINALDATE` tag), truncated
to 4 digits.  Fallback: `release.date` year.  If neither is known, omit the suffix entirely
(rather than silently using a wrong year).

**Always present** when a year is known — not only when a collision would occur.

**Collision handling:** In the extremely rare case of same-performers/same-work/same-year, the
existing file-collision detection in `run()` will surface it and prompt the user.  No further
disambiguation logic needed for now.

**Example:** `Violin Concerto in D major, Op. 61 [1962]`

### 3. Required code changes

**`models.py`:**
- Add `ordering_key: int = 0` with `Field(alias="ordering-key")` to `MBWorkRelation`.
  Pydantic coerces the string `"8"` from the MB API to `int` automatically.
- Add `ordering_key: int = 0` to `WorkHierarchyLevel`.

**`_tags.py` — `build_cwp_tags`:**
- When building each `WorkHierarchyLevel`, extract `ordering_key` from the `parts/backward`
  relation on that work (already in the `MBWork.work_relation_list`).
- Expose as `cwp_ordering_key_{i}` in `model_extra` alongside existing per-level fields.

**`_tags.py` — `build_dest_path`:**
- Replace `(work_mbid)` suffix with `[YYYY]` from `ORIGINALDATE` → `DATE` → omit.
- When `cwp_part_levels >= 2`: emit one `nn - <part_title>` subdirectory per intermediate
  level (indices 1 to n-2).
- `nn` for intermediate dirs: `cwp_ordering_key_{i}` zero-padded to 2 digits, fallback to
  1-based ordinal among siblings.
- `nn` for leaf filename: `cwp_ordering_key_0` if non-zero → `MOVEMENTNUMBER` → `track.position`.

**`_pipeline.py` — `run()` post-loop:**
- No change.  `MOVEMENTNUMBER` assignment (global composer numbering) is correct as-is.

**Tests:**
- `MBWorkRelation`: `ordering-key` string coerced to int; absent → 0.
- `WorkHierarchyLevel`: `ordering_key` field present.
- `build_cwp_tags`: `cwp_ordering_key_{i}` in `model_extra` for 3-level hierarchy.
- `build_dest_path` 2-level: `[YYYY]` replaces `(mbid)`, no intermediate dirs (regression guard).
- `build_dest_path` 2-level, no date: no `[YYYY]` suffix.
- `build_dest_path` 3-level with `ordering_key` present: correct `nn - ` intermediate dir.
- `build_dest_path` 3-level with `ordering_key=0`: fallback to ordinal.
- `build_dest_path` 4-level (depth-3 compositional hierarchy): two intermediate dirs.
- `AGENTS.md`: update test count after new tests added.

---

## Open design question — native language / native script

**Decision in principle:** names and titles should use native language and native alphabet where
known.  This is an intentional departure from conventional (Latin-transliteration-first) practice
in favour of scholarly correctness.

### Artist names (composers, conductors, performers)

**Current state:** MB already stores native script as the canonical `name` field:
- `'Пётр Ильич Чайковский'` (not `'Pyotr Tchaikovsky'`)
- `'Николай Андреевич Римский‐Корсаков'`
- `'武満徹'` (Takemitsu Tōru)

Music-annotator already uses `rel.artist.name` for the artist tags, so Cyrillic/CJK names are
already in the tags without any code change.

**Gap — composer directory component:** `build_dest_path` currently derives the composer
component from `sort-name` (always Latin transliteration) for last-name extraction via
`last_name()`.  To use native script in the directory, `name` would need to be used instead of
`sort-name`, and `last_name()` would need to be either skipped or made language-aware.

**Open question:** Should the filesystem path use native script (e.g. `Чайковский - …/`) or
retain Latin transliteration for the directory while using native script in tags?  Both are valid;
native script is more consistent with the stated goal but requires deciding how to shorten names
(last-name extraction is trivial for `Last, First` sort-names but requires language knowledge for
Cyrillic/CJK).

**Possible approach:** Use the locale-tagged `ru` (or `ja`, `zh`, etc.) `"Artist name"` alias
with `primary=primary` as the display name, and extract the last name from its `sort-name`
counterpart.  MB has this data for most major composers.

### Work titles

**Current state — uneven MB coverage:**
- Italian opera titles: already in original language (Otello, La Traviata, etc.) ✓
- German Lieder/symphonies: often in English in MB despite `language=deu` (e.g. `'Symphony no. 9
  in D minor, op. 125 "Choral"'` — English title, language=German) ✗
- Russian instrumental works: canonical title frequently in English transliteration; native title
  sometimes absent entirely from MB (e.g. Tchaikovsky Serenade for Strings has no Russian title
  alias in MB) ✗
- `'Каприччио на испанские темы, op. 34'` exists in MB as a **separate work** (the piano-duet
  original), linked to `'Capriccio Espagnol, op. 34'` (the orchestral version) via an
  `arrangement/backward` relation — they are not the same work entry with two title aliases.

**Practical conclusion:** MB's work title alias coverage is too sparse to reliably provide native
titles for most instrumental works.  A locale-aware alias lookup could help for works that have
locale-tagged aliases, but would silently fall back to English for many Russian/German/Czech works.

**Open question:** Accept partial coverage (native title when MB has it, English fallback
otherwise), or defer this topic until MB data quality improves?  If accepted, the implementation
would be:
1. Fetch work with `includes=['aliases']` (already done in `fetch_work_detail`? — check).
2. Prefer alias with `locale` matching the artist's native locale and `type="Work name"`.
3. Fall back to canonical `title` if no matching alias.

**Also needed:** MB's `language` field on `MBWork` means "language of the text set to music"
(vocal works), not "language of the title".  It cannot be used to infer title language.

---

## Open design question — top-directory performer cardinality

**Context:** The top directory is currently `<Composer lastnames> - <Conductor; Ensemble>`.
Soloists are excluded.  This gives stable grouping (all Dutoit/OSM recordings together) but
silently omits soloist variation (two different pianists performing the same concerto with the
same orchestra land in the same directory).

**Decision:** Keep the current conductor + ensemble policy for now.  The MBID is gone from the
work directory, but the recording year `[YYYY]` provides sufficient disambiguation for most cases.
If a soloist-driven collision occurs (same conductor, ensemble, work, year, different soloist),
the file-collision prompt will surface it and the policy can be revisited.

**Deferred:** Adding soloists to the top directory for concerto-type works.

---

## Open design question — multiple recordings / release disambiguation

**Resolved:** Use `[YYYY]` from `release_group.first_release_date` in the work directory (see
section 2 above).  The full MBID UUID is removed from the filesystem layout.

---

## Deferred / longer-term items

### Playlist generation for collections/cycles
The Ring cycle, Beethoven symphony cycles, etc. should be realised as **playlists** rather than
filesystem directories.  A collection-level directory layer was explicitly rejected.  Playlist
format (M3U, XSPF, or other) and generation logic TBD.

### Re-annotation / update mode
A mode to diff existing library tracks against updated MB / CAA / Discogs data and apply changes
with user confirmation.  Should check that embedded cover art is at original resolution and
replace thumbnails if found.

### Whipper integration
A mode that calls whipper (CD ripper) and passes the output directly into the annotation pipeline.
Journal events added for rip operations.

### Discogs integration
Fallback search and release creation support when MB has no entry.  Journal events added.

### AcoustID retry/backoff
The raw `urlopen()` in `fetch_acoustid_id` has no retry logic.  Should use `_mb_retry` or an
equivalent.

### `mb.get*() ; time.sleep()` consolidation
Factor the repeated `call ; sleep(1)` pattern into a single helper.

### Verify all CE-derived tags are always written
Audit that no MB-derived or CE-derived tag fields are silently dropped when their source data
is absent.  Every field should either be populated or explicitly set to `""`.

---

## Directory layout reference (agreed)

```
<dest_root>/
  <Composer lastnames> - <Conductor; Ensemble>/          ← Latin or native script (open question)
    <Work title> [YYYY]/                                  ← year from release_group.first_release_date
      [nn - <Intermediate division>/]                    ← only when hierarchy depth ≥ 3
        [nn - <Sub-intermediate division>/]              ← only when hierarchy depth ≥ 4
          nn - <Movement title>.<ext>
```

- `nn`: zero-padded 2 digits (3 if >99 siblings), directory-scoped, derived from MB `ordering-key`
  → `MOVEMENTNUMBER` → `track.position`.
- `MOVEMENTNUMBER` in tag and title string: composer's global numbering across the whole work
  (e.g. No. 39 in the Handel Messiah).  Distinct from the directory-local `nn` prefix.
- Performer component: conductor + ensemble names only (soloists excluded for now).
- Collection/cycle wrappers (Ring cycle, symphony cycles): excluded from filesystem, deferred to
  playlist generation.
- `[YYYY]` always present when a year is known; omitted if both `release_group.first_release_date`
  and `release.date` are absent.
