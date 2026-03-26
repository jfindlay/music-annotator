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
- **Work title authenticity — composer intent vs. reception history:** The principle is that
  work titles and subtitles should reflect the composer's own stated intent, not names added
  later by performers, impresarios, publishers, or reception history.  Concrete cases that will
  need resolution during the Wikipedia / IMSLP consultation phase:

  - Bach *Six concerts avec plusieurs instruments*, BWV 1046–1051: the French title is
    correct — Bach wrote it himself on the 1721 manuscript.  "Brandenburgische Konzerte" is
    a later German colloquial name and should not displace the composer's own title.  MB's
    canonical title (French) is therefore correct, and music-annotator's current behaviour
    is right.

  - Mahler Symphony no. 8, subtitle "Sinfonie der Tausend": added by impresario Emil Gutmann
    as a marketing description for the 1910 premiere.  Mahler did not give the work this
    subtitle; it is not a composer-sanctioned title element and should be excluded.

  - Schubert Symphony no. 8, subtitle "Unvollendete" (Unfinished): a posthumous descriptive
    title reflecting the incomplete state of the manuscript.  Whether Schubert intended the
    work to remain in two movements is disputed; investigate with IMSLP autograph evidence
    before deciding whether to include or exclude the subtitle.

  Implementation depends on the Wikipedia / IMSLP consultation phase for authoritative
  manuscript and critical-edition evidence.

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
