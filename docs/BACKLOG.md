# music-annotator — Backlog

Cross-cutting items that are not part of any active sharded plan and have no committed substrate yet.
Each is preserved in full so no design context is lost; when one acquires a substrate and a clear
decomposition it graduates into its own `PLAN-*.md` (or a sub-track of an existing one) and is
removed from here.  See `docs/PLAN.md` (the index) for the active plans.

---

## Source-adapter support (new ingest provenances)

- **PrestoMusic downloads.**  Source directories containing tracks downloaded from PrestoMusic.  These
  dirs may contain their own cover art and booklet, but music-annotator should still query
  MusicBrainz for a tag comparison and enrichment.  (PrestoMusic files always carry ISRCs — see
  `PLAN-fingerprint.md` F2, which activates ISRC as an identity rung.)
- **whipper and MakeMKV.**  Source-adapter support for these rippers.  whipper additionally produces
  an **AccurateRip** result (rip-fidelity against a crowd consensus of the same pressing) and a
  proper MB disc-ID from its TOC — both high-value provenance signals.  AccurateRip is the intended
  **4th archival dimension** reserved by `PLAN-fingerprint.md` (see its "Planned fourth dimension"):
  this whipper ingest mode is the source-adapter work that produces/exposes it.  When sharded, this
  becomes a source-adapters plan that `PLAN-fingerprint.md`'s rung-0 source-tag reader consumes.

## Submit disc IDs to MusicBrainz

When `parse_disc_toc` succeeds (a valid `00 - disc info.yaml` is present) but `_match_medium_by_toc`
finds no registered disc IDs on the release, music-annotator has the FreeDB CRC and sector offsets
needed to compute a proper MusicBrainz disc ID.  A future phase could offer to submit the disc ID to
MB via the `/ws/2/discid` endpoint, permanently enriching the database and enabling TOC-based
selection for all users.  Requires an authenticated MB session; defer until a login/credential flow
is designed.

## Hierarchy-depth normalisation (deferred L2 of the leaf-numbering plan)

The leaf/intermediate numbering fix (L0/L1 of the now-complete `PLAN-leafnumber.md`) shipped; the
**depth-uniformity** half (L2) was designed at an Opus-inflection HALT and then **deferred** — the
user elected not to ship depth normalisation until the library is complete and the full distribution
of depth shapes is known (designing from a maintenance position rather than the 36-group census).
The converged design is preserved as two durable rules in `docs/NOTES.md` ("Tree-to-path rendering:
two durable rules") — ragged depth has two opposite-routing sources, and the *uniform-ceiling /
ragged-floor* rule (render each leaf at `min(own tree depth, group modal depth)`: clamp
over-resolution down, never pad under-resolution up).  When reopened it materialises as an additive
pipeline pass writing `cwp_render_levels` as model_extra, consumed by `build_dest_path`'s depth
branch, falling back to raw `cwp_part_levels` when absent.

Scope when reopened (from the census of 36 non-uniform groups in 6 shapes):
- **Shape A (20 groups) — out of scope, preserve.**  Overture/sinfonia at PL=1 among PL=2 acts is
  genuinely top-level (ragged *floor*); the rule must not over-normalise it.
- **Shapes C/D (3 groups: Handel Water Music, Bach Matthäus-Passion, Haydn Schöpfung) — the target.**
  A movement has MB sub-parts (IIIa/IIIb; lettered recits) nesting deeper than flat siblings; clamp
  the over-resolution down.
- **Shape B (9 groups, mixed flat/split movements)** and **Shape F (2 groups, excerpt discs, depth
  spread {1,3})** — per-shape call deferred to reopen (likely acceptable as-is / near-arbitrary modal).
- Pinned corner cases: modal ties → shallower depth; PL=0 orphans (Shape E) excluded from the modal
  computation (see next item).
- **Reopen criteria:** when the library is complete (more depth shapes likely), or sooner if a new
  shape appears that the uniform-ceiling rule mishandles.

## PL=0 orphan tracks — hierarchy-resolution / MB-data-gap defect

Two groups in the census (Mozart Divertimento K.136 "II. Andante"; Litaniae K.243 "X. Miserere")
have a single movement whose MB work record carries **no `part of` relation**, so
`build_work_hierarchy` (`_works.py`) resolves it as a standalone top work (`CWP_PART_LEVELS=0`,
`workid_0 == workid_top`).  This is a hierarchy-resolution / MB-data-gap defect, **not** a
numbering-policy question — it was explicitly scoped out of the leaf-numbering plan's L2 so it would
not contaminate the depth-rendering policy.  Candidate fixes: a `_works.py` resolution improvement,
an MB submit-mode correction (add the missing `part of` link upstream), or an editorial allowlist.
Per the "ragged depth has two sources" rule in `docs/NOTES.md`, the defect should be kept *visible*
in the path until fixed at the data/resolution layer, never papered over in the renderer.

## Other unsharded backlog

- Playlist generation for collection/cycle groupings (Ring cycle, symphony cycles, etc.).
- Audit CE-derived tags: every field populated or explicitly `""`.
- Add cover art type: sleeve front/back.
- When an MBID does not have DiscIDs and comprises multiple media, music-annotator usually selects
  the wrong medium.  Can this be improved?
- This should have already been fixed?
  ```
  acoustid_lookup_failed [music_annotator._mb_api] attempt=0 error='The read operation timed out' recording_mbid=93200fdb-9f20-4eb0-8cc1-0aed9d97508c wait_s=1
  ```

---

## Deferred — editorial / data-source dependent

- **`[rec YYYY]` session-date label — partial:** Revisit: any improvements possible?  `[rec YYYY]` is
  activated from `RECORDING_DATE`, extracted from the `begin` dates on conductor/engineer/balance
  artist relations on the recording — the actual studio session range for most classical recordings
  (e.g. Beethoven 8, Karajan/BPO 1984).  Where MB has not populated relation begin dates, the label
  falls back to `[rel YYYY]`.  Additional session-date sources (Discogs / Wikipedia / IMSLP) can be
  added to the `rec_year` hook in `build_dest_path` once those integrations exist.
- **Work title authenticity — composer intent vs. reception history:** work titles and subtitles
  should reflect the composer's stated intent, not names added later by performers, impresarios,
  publishers, or reception history.  Cases to resolve during the Wikipedia / IMSLP phase:
  - Bach *Six concerts avec plusieurs instruments*, BWV 1046–1051: the French title is correct —
    Bach wrote it on the 1721 manuscript.  "Brandenburgische Konzerte" is a later German colloquial
    name and should not displace it.  MB's canonical (French) title is right.
  - Mahler Symphony no. 8, subtitle "Sinfonie der Tausend": added by impresario Emil Gutmann as 1910
    premiere marketing.  Not composer-sanctioned; exclude.
  - Schubert Symphony no. 8, "Unvollendete": a posthumous descriptive title; whether Schubert
    intended two movements is disputed — investigate with IMSLP autograph evidence before deciding.
  Implementation depends on the Wikipedia / IMSLP consultation phase.
- **Native language / native script (hybrid approach):** use split-last-word of the canonical `name`
  for Latin-script composers (no extra API call); for non-Latin-script composers (Unicode-block
  detection: Cyrillic, CJK) fetch the locale-tagged primary `"Artist name"` alias from MB and
  extract the last name from its native sort-name.  Fallback when no alias exists: full `name` as-is.
  Covers composer directory component, work titles, and performer names consistently.  Depends on the
  Wikipedia / IMSLP phase for authoritative urtext titles; until then MB canonical title is primary
  and English + unlocaled aliases are companion tags (`CWP_WORK_TOP_EN`, `CWP_WORK_TOP_ALT`).

---

## musicbrainzngs2 contributions (external dependency track)

`python-musicbrainzngs` (0.7.1, 2020) is effectively unmaintained — 47 open issues, 16 open PRs, no
releases since 2020.  A fork, `C0rn3j/python-musicbrainzngs2`, began modernisation in January 2026
(Python 3.10+, ruff, pyproject.toml, partial type stubs) but has not addressed the substantive bugs
or gaps music-annotator hit.  Not yet on PyPI.

music-annotator will migrate once musicbrainzngs2 reaches a stable release covering the fixes below.
Until then, local monkey-patches remain in `_mb_api.py` and are removed as each upstream fix lands.

Items are sketched at PR granularity; exact payload size decided as each is started.  Proceed
carefully and require slow human review+styling — we don't know how the maintainers will respond to
high-volume agent-written changes.

### Bug fixes (directly blocking or affecting music-annotator)

**mbngs2-1 — `_safe_read`: raise immediately on non-retryable HTTP codes.**  File: `musicbrainz.py`.
Replace the `else: retrying for now` branch with `raise ResponseError(cause=exc)`.  Any HTTP status
not 503/502/500 (transient) or 401 (auth) is permanent and should not be retried.  A 307 redirect
loop detected by `HTTPRedirectHandler` raises `HTTPError(307)`, currently triggering 8 retries
(~60 s); with this fix it raises `ResponseError` immediately.  Tests in `test_requests.py`:
`FakeOpener(exception=HTTPError(url, 307, ...))` → `ResponseError` on first attempt (no retries);
same for an arbitrary unknown code.  Local workaround to remove: `_patched_safe_read` in `_mb_api.py`.

**mbngs2-2 — `mbxml.parse_recording`: add `first-release-date` to elements list.**  File:
`mbxml.py`.  Add `"first-release-date"` to the `elements` list in `parse_recording`.  Present in the
XML, silently discarded today.  Upstream: `alastair/python-musicbrainzngs#288`.  Tests: recording XML
fixture with `first-release-date`; assert present.  Local workaround to remove:
`_patched_parse_recording` in `_mb_api.py`.

**mbngs2-3 — `mbxml`: add `type-id` to entity parser `attribs` lists.**  File: `mbxml.py`.  Add
`"type-id"` to `attribs` in `parse_area`, `parse_artist`, `parse_label`, `parse_place`,
`parse_event`, `parse_instrument`, `parse_release_group`, `parse_series`, `parse_work` (9 functions;
`parse_relation` already has it).  Present in XML, discarded today.  Upstream:
`alastair/python-musicbrainzngs#276`.  Tests: update affected XML fixtures; assert present.

### Modernisation (C0rn3j's stated goals)

**mbngs2-4 — Full codebase typing.**  Add type annotations throughout `musicbrainz.py`, `mbxml.py`,
`caa.py`, `util.py`, `compat.py`.  Use `from __future__ import annotations`.  Add `py.typed`.
Coordinate with C0rn3j's issue #6.

**mbngs2-5 — Remove `*` imports from `__init__.py`.**  Replace `from musicbrainzngs.caa import *` and
`from musicbrainzngs.musicbrainz import *` with explicit named exports.  Coordinate with issue #5.

**mbngs2-6 — Comprehensive test coverage.**  The suite is sparse: many paths untested (all
`_safe_read` except-clause branches, CAA redirect/error paths, edge cases in every `parse_*`).
Extend `test_requests.py`, `test_caa.py`, the `test_mbxml_*.py` modules.  Scope after mbngs2-4.

**mbngs2-7 — Address upstream open issues and PRs.**  Triage `alastair/python-musicbrainzngs` for
applicability.  Candidates: #266 (genre parsing), #282 (missing attributes), #283 (alias-list on
recordings/releases), #289 (add alias list), #291 (release-group-status parameter).  Coordinate with
issue #8.

**mbngs2-8 — Replatform on the MB API v2 XML contract.**  Cross-reference every `parse_*` in
`mbxml.py` against the MMD 2.0 RelaxNG schema
(`https://github.com/metabrainz/mmd-schema/blob/master/schema/musicbrainz_mmd-2.0.rng`).  For each
entity: verify all attributes, child elements, and list wrappers are parsed; add fields present in
the schema but absent from the parser (mbngs2-2/-3 fix the two known); remove fields no longer in the
schema.  Most open-ended; follows mbngs2-1 through mbngs2-3.
