# music-annotator — Plan

## Next up

_(nothing immediately queued)_

---

## Open questions (decision needed before implementation)

---

## Backlog

- `musicbrainzngs` → `musicbrainzngs2` migration (fork is new; may contribute upstream)

---

## Deferred

- **`[rec YYYY]` session-date label:** The `rec_year` hook in `build_dest_path` is reserved for a
  future data source (Discogs / Wikipedia / IMSLP) that provides actual studio or concert session
  dates.  All three MB date fields are publication-era years and produce `[rel YYYY]`.
- **Native language / native script (hybrid approach):** Use split-last-word of the canonical
  `name` field for Latin-script composers (no extra API call); for non-Latin-script composers
  (detected via Unicode block, e.g. Cyrillic, CJK), fetch the locale-tagged primary
  `"Artist name"` alias from MB and extract the last name from its native sort-name.  Fallback
  when no alias exists: use the full `name` as-is.  Covers composer directory component,
  work titles, and performer names consistently.  Depends on the Wikipedia / IMSLP phase for
  authoritative urtext work title strings; until then MB canonical title is primary and English +
  unlocaled aliases are stored as companion tags (`CWP_WORK_TOP_EN`, `CWP_WORK_TOP_ALT`).
- Add support for source directories containing tracks downloaded from PrestoMusic.  These dirs will potentially contain their
  own coverart and booklet.  These arts should supplant whatever is in MusicBrainz, but in copying them from `src_dir` to
  `dst_dir`, music-annotator should still try to query MusicBrainz for a tag comparison and enrichment.
- Playlist generation for collection/cycle groupings (Ring cycle, symphony cycles, etc.)
- Re-annotation / update mode: diff library against updated MB / CAA / Discogs data, replace thumbnail cover art with
  original-resolution
- Whipper integration (rip → annotate pipeline)
- Discogs integration (fallback search and release creation)
- Audit CE-derived tags: every field populated or explicitly `""`
- Performer cardinality: add soloists to top-level directory for concerto-type works
