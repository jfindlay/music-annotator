# music-annotator — Plan

## Next up

_(nothing immediately queued)_

---

## Open questions (decision needed before implementation)

- **Directory composer last-name in native script:** choose between alias-based lookup
  (correct for all scripts, extra MB call per artist) vs. split-last-word heuristic
  (simple, wrong for CJK) vs. hybrid.

---

## Backlog

- `musicbrainzngs` → `musicbrainzngs2` migration (fork is new; may contribute upstream)

---

## Deferred

- Add support for source directories containing tracks downloaded from PrestoMusic.  These dirs will potentially contain their
  own coverart and booklet.  These arts should supplant whatever is in MusicBrainz, but in copying them from `src_dir` to
  `dst_dir`, music-annotator should still try to query MusicBrainz for a tag comparison and enrichment.
- Native work title primary selection — consult Wikipedia / IMSLP for authoritative urtext strings; until then MB canonical
  title is primary, aliases stored as companions
- Playlist generation for collection/cycle groupings (Ring cycle, symphony cycles, etc.)
- Re-annotation / update mode: diff library against updated MB / CAA / Discogs data, replace thumbnail cover art with
  original-resolution
- Whipper integration (rip → annotate pipeline)
- Discogs integration (fallback search and release creation)
- Audit CE-derived tags: every field populated or explicitly `""`
- Performer cardinality: add soloists to top-level directory for concerto-type works
