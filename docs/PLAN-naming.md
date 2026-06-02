# music-annotator — Plan: Library-wide Dir/File-naming Unification

This is a **pre-shard plan**, not yet session-sharded for `/run-plan`.  Its sessions are crisply
known only after its two substrates land, so it is described at sub-track granularity per the
multi-session-planning manual (`~/.config/opencode/multi-session-planning.md`); full original design
prose is preserved so no context is lost.  When the substrates land, this file is re-written into
the standard sharded format (session list, contracts, ledger, action-frame digest) and executed by
`@plan-admin` via `/run-plan`.

This is one of several independent plans — see `docs/PLAN.md` (the index) for the full set.

---

## Purpose (design intent)

A full pass over the **already-annotated** library to unify dir/file naming structure: works that
got split across directory-name variations (different language, misspelling, per-track credit
differences) are reconciled to one canonical handle, and structural prefixing artefacts are cleaned
up retroactively.

This is the library-grouping/maintenance counterpart to the per-release path logic: where the
multi-medium plan makes *new* annotations correct, this plan makes the *existing* library
*consistent*.  Some of this was impossible when music-annotator was a stateless cursor seeing one
medium per step; it is feasible only with whole-library context.

---

## Dependencies (why this is not yet sharded)

This plan consumes two substrates and should not be sharded until both have landed:

- **`docs/PLAN-multimedium.md` S0 (cross-medium work-groups, contract C-S0).**  Work-level
  unification must operate over cross-medium groups, never per-medium.  S1 (composer unification)
  and S2/S3 (dates, `dd.dd` prefix) establish the *per-release* versions of the passes this plan
  generalises *library-wide*.  Any work-spanning normalisation here must consume C-S0 rather than
  reintroduce single-medium aggregation.
- **`docs/PLAN-fingerprint.md` identity layer (the archival triple; `audit` machinery).**  Detecting
  that two differently-named directories are the *same* work/recording is an identity question; the
  reliable join key is the embedded tag (`MUSICBRAINZ_ALBUMID` / `MUSICBRAINZ_WORKID` /
  `acoustid_id`), per NOTES "journal detects, tag adjudicates".  The library-wide retroactive passes
  here are naturally modes of the same `audit` subcommand that plan introduces, and any move they
  perform must re-journal (NOTES corollary).

---

## Sub-tracks (granularity — re-shard when substrates land)

### N1 — Unify works split by directory-name variation

Works split across directories by different language, misspelling, or punctuation variation are
reconciled to one canonical handle.  The join key is the embedded MB work/release tag, not the
directory name (a *handle, not a manifest* — a length-shortened or manually-renamed dir loses any
embedded key; see NOTES.md).

- **Arranger/finisher in directory path — work-level only.**  When a finisher/completion/arranger is
  credited as `"composer"` with the `"additional"` attribute on only some movements, the per-track
  `role_buckets.composers` is empty for those movements and `effective_composers` falls back to
  `additional_composers`, producing a different `CWP_COMPOSER_LASTNAMES` — and a different `top_dir`
  — than movements with a plain primary-composer relation (e.g. Mozart K. 626 Süßmayr movements land
  in `Mozart; Süßmayr - …`).  `PLAN-multimedium.md` S1 fixes this *within and across media* via the
  cross-medium composer pass for *new* annotations.  This plan's job is the **library-wide
  retroactive pass** over already-annotated dirs.  Any arranger-style path credit must be applied at
  the work level (group-wide aggregated value), never per-track.
- **Multi-medium limitation (inherited).**  `PLAN-multimedium.md` S0 removes the per-medium
  limitation for the `recording_date_work` union, `recording_first_release_date` normalisation, and
  composer unification.  Any work-spanning normalisation in this plan must consume C-S0.

### N2 — Retroactive `dd.dd` leaf-prefix cleanup

`PLAN-multimedium.md` S3 mechanically fixes the `dd.dd` over-application (prefix added to some
multitrack works that are not partial-performance collisions) for *new* annotations.  This sub-track
is the **library-wide retroactive pass** over already-annotated dirs that carry the stale prefix.
Must not regress the legitimate partial-performance-collision case the prefix exists for.

### N3 — Re-annotation / update-diff mode

An update-diff function to capture tag improvements or additive cover art: diff the library against
updated MusicBrainz / Cover Art Archive / Discogs / Wikipedia data and apply the additive
improvements.  Depends on those external integrations existing (several are `docs/BACKLOG.md`
items).

### N4 — User-improvement mode

- The user adds cover art for a release.
- music-annotator extracts metadata (dates, producers, performers, etc.) from the cover art and
  updates the appropriate tags.

(Also touches "additional ensembles attributed to a single track in a multitrack work could fork the
written paths for those tracks" — a whole-library-context observation; resolve its path-vs-tag
treatment through the Classical Extras anchor when sharded.)

---

## Invariants this plan must observe (named elsewhere)

- **Path is a handle, not a manifest** (`NOTES.md`).  Unification reconciles *handles*; full credits
  stay in tags.
- **Journal detects, tag adjudicates** (`NOTES.md`).  Use the journal to flag candidate
  fragment-groups cheaply, confirm via the embedded tag, and re-journal any move.
- **Classical Extras as editorial anchor** (`NOTES.md`).  Every reconciliation decision refracts
  through CE.
- **Hash anchors, identity floats** (`docs/NOTES.md` archival identity invariants, P-FP1).  When this plan uses identity to
  group, the `audio_hash` anchor proves "same audio" independent of fallible cluster IDs.
