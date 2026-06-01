# music-annotator — design notes and learnings

## Path is a handle, not a manifest

The destination directory and filename are *handles* — short, stable identifiers a user
locates a recording by — not *manifests* of every credited contributor.  Full credits
belong in tags.  The implication is structural and applies wherever path construction
touches metadata:

- **Performer component of the directory.** Source is ``release.artist_credit`` (the
  curator's resolution of primary attribution), not the union of every per-track credit.
  Named subgroups, additional choirs, and session soloists belong in tags, not the path.
  Concerto-like works are the exception: when the soloist is part of the work's canonical
  identity, it enters the path regardless of release-level crediting.
- **UX ceilings are real.** Music-player display widths and filesystem path limits cap the
  useful length of a dir/file name.  Any policy that admits unbounded credits into the path
  will eventually exceed both.  Mahler 8 with full credits is the worst case; the policy
  must keep that case readable.
- **Every dimension added to path construction needs a unification story.** MB credits
  movements of one recording inconsistently.  When a path-construction dimension can vary
  across movements of one work-group (composer with arranger/finisher attribute,
  recording-date interval, conductor/ensemble set), the existing pattern is a work-level
  unification pass in ``_pipeline.py`` — see the composer pass at lines 910-945 and the
  ``recording_date_work`` pass at lines 947-980.  Release-scoped sources (album-artist
  filter, release date) don't need a unification pass because they're already uniform.

## Join key: journal detects, tag adjudicates

For any library-grouping or regrouping work, choose the join key by whether the feature *mutates*
library state:

- The **transaction journal** is the cheapest detector — it already pairs each file's full release
  MBID with its destination, readable in one parse with no per-file tag scan.  But it records *past
  actions*, not present state: it goes stale the moment a directory is moved out-of-band (and a
  regrouping feature's whole job is to move things).  It is also blind to files that arrived by any
  path other than a logged ``run()``.
- The embedded **``MUSICBRAINZ_ALBUMID`` tag** is the only *present-state* authority — it travels
  with the file through every move and arrives with the file regardless of how the file got there.
  But scanning it library-wide is expensive (mutagen-parse every file).
- The **directory name** is a *handle, not a manifest* (see above) — a lossy index at best; a
  length-shortened or manually-renamed dir loses any embedded key.

The resulting rule: **journal detects, tag adjudicates.**  Use the journal to flag candidate
fragment-groups cheaply, then read the tag on just those candidates to confirm before acting.  A
join key that goes stale the moment you act on it is the wrong *authority* for a feature whose
purpose is to act — but it is fine as the *trigger*.  Corollary: any maintenance action that moves a
directory must append its own journal entry, or the detector decays with use.

## Leaf-numbering bug: ordering-key is per-work, not per-track (the Mahler-9 phenomenology)

Confirmed against a real output dir (Karajan/BPO Mahler Symphony no. 9, single-work album where each
movement is split into many sub-section tracks).  Three symptoms, one root cause plus two
consequences:

**Root cause — the leaf ``nn`` is the bottom-work's ordering-key, which is the MOVEMENT number, not
the track's position within the movement.**  ``build_dest_path`` sets ``leaf_nn`` from
``CWP_ORDERING_KEY_0`` when it is > 0 (`_tags.py:1064/1078`).  For a symphony whose movements are
each split into N recordings, every recording of movement I carries the *same* bottom work and
therefore the *same* ``ordering_key_0`` (= 1).  So all 8 sub-sections of movement I want leaf ``01``,
all of movement II want ``02``, etc.  The ordering-key answers "which movement" not "which track".

**Symptom 1 — title collapse.**  The colliding leaves are distinguished only by ``TITLE``
(``"…: I. Andante comodo"`` vs ``"…: I. Etwas frischer"``).  But the realised filenames show the
title collapsed to ``"Symphonie no. 9_ I"`` for the deduped tracks — the distinguishing subtitle is
gone.  (Exact collapse path still to confirm: ``safe_name`` does not truncate, so the collapse comes
either from ``_dedup_plan_entries`` taking ``rest`` from the *already-colliding* stem, or from
``_resolve_long_names`` Strategy 2 dropping everything after ``" _ "``.  The interaction of dedup
(runs first, `_pipeline.py:1119`) and long-name resolution (`_pipeline.py:1126`) is the suspect.)

**Symptom 2 — ``dd.dd`` over-application.**  ``_dedup_plan_entries`` is itself mechanically correct:
it fires *only* on byte-identical destination paths.  But the per-work ordering-key (root cause) +
title collapse (symptom 1) manufacture those collisions, so ``dd.dd`` fires on what is really a
legitimate run of distinct sub-sections.  The earlier backlog framing ("dd.dd added to works that
are NOT partial-performance collisions, fix _dedup_plan_entries") was **wrong about the locus** — the
dedup function is downstream of the real bug.

**Symptom 3 — broken playback order.**  The ``dd`` after the dot is ``CopyPlanEntry.idx + 1`` (the
global running index across the actioned medium), which does *not* restart per movement, so movement
II's deduped tracks read ``02.10 … 02.14``.  Worse, any track that *escapes* dedup (because its title
happened not to collapse, e.g. ``trk=4 "I. Mit Wut"``) keeps a bare ``01`` and sorts *before* the
``01.0x`` files (space ``0x20`` < dot ``0x2e``), and its global index (``04``) never appears in the
``01.dd`` sequence.  Net: files do not sort in playback order, and the numbering has gaps.

**Design implication (for the subsequent fix multi-session, not yet designed):** the leaf number must
encode *track position within the work-group*, not the bottom-work ordering-key, when a single work
is split across many tracks.  Candidate fixes (to be evaluated, not yet chosen): use the per-group
movement index already computed in the unification pass (``cwp_movt_num``); or make the leaf a
two-level ``movement.subsection`` derived from the ordering-key *plus* intra-movement sequence; or
preserve the full distinguishing title and let it (not ``dd.dd``) carry uniqueness.  All three must
refract through "path is a handle, not a manifest" and must produce a stable, playback-sorted,
gap-free sequence.  **Further phenomenology expected** — other split-work shapes (multi-disc works,
works with mixed split/unsplit movements, opera tracks) may surface additional cases; collect real
examples before freezing the fix design.

## Classical Extras as editorial anchor

Every editorial decision on attribution, annotation, and path construction must refract
through the Classical Extras conventions
(github.com/metabrainz/picard-plugins/tree/2.0/plugins/classical_extras).  CE is not just
a tag-format spec — it encodes a coherent stance on how art-music recordings should be
described.  When a new rule (path-vs-tag split, soloist promotion, subgroup demotion) is
proposed, the validation step is: does CE's framing of the same question agree, and if
not, what's the principled reason for diverging?  The default is to follow CE; divergences
need a documented rationale.
