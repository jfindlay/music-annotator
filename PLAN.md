# music-annotator — Plan

## Backlog

- **Directory path — concerto-like soloist override:** The album-artist filter for conductors and
  ensembles is implemented (`cea_album_conductors_list` / `cea_album_ensembles_list` in
  `build_dest_path`).  The remaining open item is soloists: when the soloist is part of the work's
  canonical identity — concertos, works like Saint-Saëns Symphony no. 3 (organ) or "Cinema
  Serenade" (violin) — the soloist should enter the directory even when not credited at release
  level.  Detection is partially mechanical: ``top_work.type == "Concerto"`` covers concertos
  directly.  Symphonies-with-soloist and other canonical soloist-feature works are harder; candidate
  signals include "solo X" instrument relation types, dedicated work-title patterns, or an editorial
  allowlist.  The rule answers "is the soloist part of the work's canonical identity?" not "is the
  soloist on the release?".  All decisions must refract through the Classical Extras path-vs-tag
  distinction (primary attribution in path, full credits in tags).  See NOTES.md "Path is a handle,
  not a manifest."
- **Codebase audit:** As the project grows, do a thorough review of principles, structure, and
  goals.  Evaluate whether the module boundaries remain natural, whether the public API surface in
  `__init__.py` is still coherent, and whether any accumulated conventions need revisiting.
- **Library integrity mode:** Some tasks might have been possible with music-annotator operating as
  a stateless cursor on the library that sees only one medium per step, and many other tasks are
  easier or only possible knowing the context.  For example, additional ensembles only attributed to
  a single track in a multitrack work could fork the written paths for those tracks.  Other tasks
  are outside of music-annotator's original design goal: to annotate and catalog the library.
  - Full pass on the entire library to unify dir/file naming structure.  Specifically:
    - Unify works split by directory name variations
      - Tracks within the work name the work differently, for example in a different language,
        misspelling, etc.
      - **Arranger/finisher in directory path — work-level only:** When a finisher, completion
        credit, or arranger is credited in MB as `"composer"` with the `"additional"` attribute on
        only some movements, the per-track `role_buckets.composers` is empty for those movements and
        `effective_composers` falls back to `additional_composers`, producing a different
        `CWP_COMPOSER_LASTNAMES` — and therefore a different `top_dir` — than movements that carry a
        plain primary-composer relation.  Example: Mozart K. 626 movements completed by Süßmayr land
        in `Mozart; Süßmayr - …` while the rest land in `Mozart - …`.  The cross-track composer
        unification pass in `_pipeline.py` (analogous to the `recording_date_work` pass) fixes this
        within a single medium.  Any future change that adds new arranger-style credits to the
        directory must be applied at the work level (using the group-wide aggregated value) rather
        than per-track.
      - **Multi-medium limitation — per-medium processing:** music-annotator processes one medium
        (disc) at a time.  Any post-processing that aggregates across all movements of a work is
        silently incorrect or absent when those movements span multiple media.  Affected operations
        include: the `recording_date_work` union label (`_pipeline.py` lines 757–825), the
        `recording_first_release_date` normalisation, the composer unification pass, and any future
        work-spanning normalisations.  A future phase will need a cross-medium aggregation pass, or
        the pipeline will need to be restructured to collect all media for a release before running
        these post-processing steps.
    - Fix the `dd.dd` prefixing added to some multitrack works
  - An update diff function to capture any tag imrprovements or additive cover art.
    - Re-annotation / update mode: diff library against updated MB / CAA / Discogs data / Wikipedia
  - User improvement mode:
    - User adds cover art for a release
    - music-annotator extracts metadata (dates, producers, performers, etc.) from cover art and
      updates to the appropriate tags
- **Submit disc IDs to MusicBrainz:** When `parse_disc_toc` succeeds (a valid `00 - disc info.yaml`
  is present) but `_match_medium_by_toc` finds no registered disc IDs on the release,
  music-annotator has the FreeDB CRC and sector offsets needed to compute a proper MusicBrainz disc
  ID.  A future phase could offer to submit the disc ID to MB via the `/ws/2/discid` endpoint,
  permanently enriching the database and enabling TOC-based selection for all users.  This requires
  an authenticated MB session; defer until a login/credential flow is designed.
- Add support for source directories containing tracks downloaded from PrestoMusic.  These dirs will
  potentially contain their own coverart and booklet, but music-annotator should still try to query
  MusicBrainz for a tag comparison and enrichment.
- Add support for whipper and MakeMKV
- Playlist generation for collection/cycle groupings (Ring cycle, symphony cycles, etc.)
- Audit CE-derived tags: every field populated or explicitly `""`
- Add cover art type: sleeve front/back

---

## Deferred

- **`[rec YYYY]` session-date label — partial:** Revisit this: are there any improvements that can
  be made? `[rec YYYY]` is now activated from `RECORDING_DATE`, which is extracted from the `begin`
  dates on conductor/engineer/balance artist relations on the recording.  This gives the actual
  studio session date range for most classical recordings (e.g. Beethoven 8, Karajan/BPO 1984).  For
  recordings where MB has not populated relation begin dates, the label falls back to `[rel YYYY]`
  as before.  Additional session date sources (Discogs / Wikipedia / IMSLP) can be added to the
  `rec_year` hook in `build_dest_path` when those integrations are implemented.
- **Work title authenticity — composer intent vs. reception history:** The principle is that work
  titles and subtitles should reflect the composer's own stated intent, not names added later by
  performers, impresarios, publishers, or reception history.  Concrete cases that will need
  resolution during the Wikipedia / IMSLP consultation phase:

  - Bach *Six concerts avec plusieurs instruments*, BWV 1046–1051: the French title is correct —
    Bach wrote it himself on the 1721 manuscript.  "Brandenburgische Konzerte" is a later German
    colloquial name and should not displace the composer's own title.  MB's canonical title (French)
    is therefore correct, and music-annotator's current behaviour is right.

  - Mahler Symphony no. 8, subtitle "Sinfonie der Tausend": added by impresario Emil Gutmann as a
    marketing description for the 1910 premiere.  Mahler did not give the work this subtitle; it is
    not a composer-sanctioned title element and should be excluded.

  - Schubert Symphony no. 8, subtitle "Unvollendete" (Unfinished): a posthumous descriptive title
    reflecting the incomplete state of the manuscript.  Whether Schubert intended the work to remain
    in two movements is disputed; investigate with IMSLP autograph evidence before deciding whether
    to include or exclude the subtitle.

  Implementation depends on the Wikipedia / IMSLP consultation phase for authoritative manuscript
  and critical-edition evidence.
- **Native language / native script (hybrid approach):** Use split-last-word of the canonical `name`
  field for Latin-script composers (no extra API call); for non-Latin-script composers (detected via
  Unicode block, e.g. Cyrillic, CJK), fetch the locale-tagged primary `"Artist name"` alias from MB
  and extract the last name from its native sort-name.  Fallback when no alias exists: use the full
  `name` as-is.  Covers composer directory component, work titles, and performer names consistently.
  Depends on the Wikipedia / IMSLP phase for authoritative urtext work title strings; until then MB
  canonical title is primary and English + unlocaled aliases are stored as companion tags
  (`CWP_WORK_TOP_EN`, `CWP_WORK_TOP_ALT`).

---

## musicbrainzngs2 contributions

`python-musicbrainzngs` (0.7.1, 2020) is effectively unmaintained — 47 open issues, 16 open PRs, no
releases since 2020, maintainer's own PRs open since January 2022.  A fork,
`C0rn3j/python-musicbrainzngs2`, began modernisation in January 2026 (Python 3.10+, ruff,
pyproject.toml, partial type stubs) but has not yet addressed any of the substantive bugs or gaps
that music-annotator has encountered.  The package is not yet on PyPI.

music-annotator will migrate its dependency to musicbrainzngs2 once it reaches a stable release
covering the fixes below.  Until then, local monkey-patches remain in `_mb_api.py` and are removed
as each upstream fix lands.

The items below are sketched at PR granularity; exact payload size will be decided as each is
started.

Also, we should proceed carefully and require a slow human review+styling of the changes as we don't
know how the project maintainers will respond to high volume agent-written changes.

### Bug fixes (directly blocking or affecting music-annotator)

**mbngs2-1 — `_safe_read`: raise immediately on non-retryable HTTP codes**

File: `musicbrainz.py`.  Replace the `else: retrying for now` branch with `raise
ResponseError(cause=exc)`.  Any HTTP status that is not 503/502/500 (transient server errors) or 401
(auth) is a permanent failure that should not be retried.  A 307 redirect loop detected by Python's
`HTTPRedirectHandler` raises `HTTPError(307)` which currently triggers 8 retries (~60 s); with this
fix it raises `ResponseError` immediately.

Tests to add to `test_requests.py`: `FakeOpener(exception=HTTPError(url, 307, ...))` → asserts
`ResponseError` is raised on the first attempt (no retries); same for an arbitrary unknown code.

Local workaround to remove once merged: `_patched_safe_read` in `_mb_api.py`.

**mbngs2-2 — `mbxml.parse_recording`: add `first-release-date` to elements list**

File: `mbxml.py`.  Add `"first-release-date"` to the `elements` list in `parse_recording`.  Field is
present in the MB XML response but silently discarded today.  Upstream issue:
`alastair/python-musicbrainzngs#288`.

Tests: add a recording XML fixture with `first-release-date`; assert field present in result.

Local workaround to remove once merged: `_patched_parse_recording` in `_mb_api.py`.

**mbngs2-3 — `mbxml`: add `type-id` to entity parser `attribs` lists**

File: `mbxml.py`.  Add `"type-id"` to `attribs` in `parse_area`, `parse_artist`, `parse_label`,
`parse_place`, `parse_event`, `parse_instrument`, `parse_release_group`, `parse_series`,
`parse_work` (9 functions; `parse_relation` already has it).  Field is present in MB XML responses
but silently discarded today.  Upstream issue: `alastair/python-musicbrainzngs#276`.

Tests: update affected XML fixtures to include `type-id` attribute; assert field present.

### Modernisation (C0rn3j's stated goals)

**mbngs2-4 — Full codebase typing**

Add type annotations throughout `musicbrainz.py`, `mbxml.py`, `caa.py`, `util.py`, `compat.py`.  Use
`from __future__ import annotations`.  Add `py.typed` PEP 561 marker.  Coordinate with C0rn3j's
existing issue #6 to avoid duplication.

**mbngs2-5 — Remove `*` imports from `__init__.py`**

Replace `from musicbrainzngs.caa import *` and `from musicbrainzngs.musicbrainz import *` with
explicit named exports.  Coordinate with C0rn3j's issue #5.

**mbngs2-6 — Comprehensive test coverage**

Current suite is sparse: fixture XML exists for some entities but many code paths are untested (all
`_safe_read` except-clause branches, CAA redirect and error paths, edge cases in every `parse_*`
function).  Extend `test_requests.py`, `test_caa.py`, and the `test_mbxml_*.py` modules.  Scope to
be determined after mbngs2-4 (typing) clarifies the code structure.

**mbngs2-7 — Address upstream open issues and PRs**

Triage `alastair/python-musicbrainzngs` open issues and PRs for applicability to mbngs2.  Notable
candidates: #266 (genre parsing), #282 (missing attributes), #283 (alias-list on
recordings/releases), #289 (add alias list), #291 (release-group-status parameter).  Coordinate with
C0rn3j's issue #8.

**mbngs2-8 — Replatform on the MB API v2 XML contract**

Cross-reference every `parse_*` function in `mbxml.py` against the authoritative MMD 2.0 RelaxNG
schema at `https://github.com/metabrainz/mmd-schema/blob/master/schema/musicbrainz_mmd-2.0.rng`.
For each entity: verify all XML attributes, child elements, and list wrappers are parsed; identify
and add any fields present in the schema but absent from the parser (mbngs2-2 and mbngs2-3 fix the
two we already know about); remove any fields that are no longer in the schema.  This is the most
open-ended item and should follow mbngs2-1 through mbngs2-3.
