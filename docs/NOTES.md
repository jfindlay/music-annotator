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

## Leaf-numbering invariants (resolved by `docs/PLAN-leafnumber.md`)

Four invariants govern leaf and intermediate numbering.  Sessions L0–L4 of the leaf-numbering plan
resolved and froze them; L2 was designed but deferred.

**Invariant 1 — Leaf `nn` = per-group track index (`CWP_MOVT_NUM`), not the bottom-work ordering-key
(`CWP_ORDERING_KEY_0`).**  `build_dest_path` derives the leaf from `CWP_MOVT_NUM` — the gap-free,
playback-ordered, disc-spanning 1-based position within the unified top-work group, set by the
`top_work_groups` pass in `_pipeline.py`.  `CWP_ORDERING_KEY_0` is not used for the leaf.  This is
the **permanent authority**: it is correct under both MB data shapes (one recording per bottom work,
or many), and a future MB-data correction that makes ordering-keys per-recording-distinct does NOT
license reverting — doing so would recouple path stability to remote-data quality.  (Contract C-L0,
frozen by L0 commit `011490a`.)

**Invariant 2 — Intermediate `nn` = per-group sibling index (`CWP_INTER_INDEX_{i}`), gap-free per
parent.**  For each intermediate level `i ≥ 1`, the pipeline enumerates distinct `cwp_workid_{i}`
values that share a parent, ranks them by ascending `cwp_ordering_key_{i}`, and assigns a gap-free
1-based sibling index stored as `cwp_inter_index_{i}` on every track of that node.
`build_dest_path`'s intermediate-dir loop consumes `CWP_INTER_INDEX_{i}` for the `nn`, falling back
to the raw ordering-key only when the index is absent (no-group escape hatch).  (Contract C-L1,
frozen by L1 commit `c8ee525`.)

**Invariant 3 — One rendering depth per work-group (uniform-ceiling / ragged-floor rule) — DESIGNED,
DEFERRED (L2).**  The policy was designed at the 2026 Opus-inflection HALT (see "Tree-to-path
rendering: two durable rules" below and the L2 Discovery in `docs/PLAN-leafnumber.md`) but the user
elected not to ship depth normalisation until the library is complete and the full distribution of
depth shapes is known.  The leaf/intermediate numbering fix (Invariants 1 and 2) shipped; depth
normalisation is parked.  The `repath` subcommand (Invariant 4) brings the library forward for the
numbering change.  **Do not claim depth normalisation is implemented.**  (Prose invariant P-L2,
deferred at L2.)

**Invariant 4 — `repathed` journal obligation.**  A path-policy change is retroactive.  The `repath`
subcommand brings the existing library forward: it recomputes each file's destination from embedded
tags alone (offline, no MusicBrainz fetch) and moves files to corrected paths.  Every move is
journalled as `action="repathed"` with the same SHA + tag-round-trip provenance as ingest — SHA of
source captured before move, move executed, SHA of destination verified equal, `_verify_copy` tag
round-trip verified, ONLY THEN the journal entry appended.  A crash leaves a complete audit trail of
what already moved.  (Contract C-L4, frozen by L4 commit `f1ab378`.)

## Leaf-numbering & non-uniform-depth bugs (corrected diagnosis across four work shapes)

Re-diagnosed against the **current** code (commit `86c47bf`) by reading the real on-disk tags from
four output dirs — Mahler 9 (Karajan/BPO), Wagner Meistersinger, Handel Water Music, Mozart Così fan
tutte — plus the clean counter-example Bach h-Moll-Messe.  This **supersedes** the earlier Mahler-only
entry: two of its three "symptoms" turned out to be stale artifacts of superseded code, and a fourth,
independent bug (non-uniform hierarchy depth) surfaced that the single-example diagnosis missed.

**The crisp invariant the whole featureset turns on.**  The leaf ``nn`` is the bottom-work's
``CWP_ORDERING_KEY_0`` (`_tags.py:1064/1078`).  This is correct **iff each MB bottom work maps to
exactly one recording**.  It breaks **iff one MB bottom work contains multiple recordings** — then
they all share one ordering-key and collide.  The clean Bach Mass (``ORDERING_KEY_0`` runs 1..27
gap-free across two discs, one recording per movement-work) proves the mechanism is right when the
precondition holds; the Mahler/Wagner cases break it because MB groups every sub-section of a movement
or scene under a *single* bottom work.

**Finding 1 (CONFIRMED, root cause) — the ordering-key numbers the work-node, not the track.**  For
Mahler 9 every one of the 8 sub-sections of movement I carries ``CWP_WORK_0 = "…: I. Andante comodo"``
and ``CWP_ORDERING_KEY_0 = 1``; so all 8 want leaf ``01``, all of movement II want ``02``, etc.  Same
shape one level deeper in Wagner: every recording of Akt I Scene I shares ``ORDERING_KEY_0 = 1``,
Scene II shares ``= 2``.  The ordering-key answers "which movement/scene", never "which track".

RESOLVED (L0/L3): `build_dest_path` now reads `CWP_MOVT_NUM` (the per-group track index) for the
leaf, not `CWP_ORDERING_KEY_0`.  The per-group index is the sole numbering authority; `_dedup_plan_entries`
was fully removed in L3 (commit `b5ef8e8`) so there is no second renumbering path.  See contract C-L0
and the "Leaf-numbering invariants" section above.

**Finding 2 (CORRECTS old Symptom 1) — title collapse is a STALE ARTIFACT, not a current bug.**  The
on-disk ``"Symphonie no. 9_ I"`` (distinguishing subtitle gone) was produced by an *old* ``safe_name``
that truncated; commit `9db47ab` ("Remove safe_name length cap") removed it.  Running current
``safe_name``/``_proposed_short`` on the real titles preserves them in full (they are ~40 bytes, far
under the 255-byte ``_NAME_MAX``).  No fix is needed for title collapse — and any future diagnosis off
on-disk output must first confirm the current code reproduces the artifact (see the meta-lesson below).

**Finding 3 (CORRECTS old Symptom 2) — ``_dedup_plan_entries`` NO LONGER FIRES, which makes the bug
WORSE.**  Because titles no longer collapse, the leaf destinations are no longer byte-identical, so
the dedup pass (which only triggers on identical paths, `_pipeline.py:681-683`) never runs for this
case.  Current output for Mahler movement I would be **eight files all numbered ``01``**, separated
only by subtitle and therefore sorted alphabetically by subtitle, not by performance order.  The
``dd.dd`` machinery is now effectively dead for split-work numbering.  The earlier "fix
_dedup_plan_entries" framing was doubly wrong: dedup is both downstream of the real bug *and* no
longer reachable.

RESOLVED (L0/L3): the dedup pass and all `.dd` machinery were fully removed in L3 (commit `b5ef8e8`).
The per-group index from C-L0 makes it unnecessary: distinct titles now get distinct leaf numbers by
construction.  Genuine byte-identical destinations are still guarded by the separate acoustid+length-aware
collision machinery (`_assess_collisions` / `_apply_collision_suffix`).

**Finding 4 (NEW — not visible from Mahler alone) — non-uniform hierarchy depth fragments one work.**
Handel Water Music Suite no. 1: most movements are ``CWP_PART_LEVELS = 2`` and land as flat files in
``01 - Water Music Suite no. 1``, but movement III has MB sub-parts (IIIa/IIIb) so its three
recordings are ``CWP_PART_LEVELS = 3`` and get an **extra intermediate directory**
(``03 - III. Allegro - Andante - Allegro da capo``) sitting *among* the flat sibling files.
``build_dest_path`` honours each track's own depth independently, so one suite is split across mixed
nesting levels with colliding/gapped leaf numbers.  **The user confirms this is a recurring MB+CE
data pattern, not a one-off** — depth normalisation within a work-group is a first-class design
dimension, not a footnote.

DEFERRED (L2): the leaf/intermediate numbering fix (L0/L1) shipped and corrects the leaf-collision
symptom.  Depth normalisation itself was designed at the 2026 Opus-inflection HALT (uniform-ceiling /
ragged-floor rule — see "Tree-to-path rendering: two durable rules" below) but the user elected to
defer shipping until the library is complete and the full depth-shape distribution is known.  The
`repath` subcommand (L4) brings the library forward for the numbering change; depth normalisation
remains parked.  See Invariant 3 above and the L2 Discovery in `docs/PLAN-leafnumber.md`.

**Design implication (for the fix plan — see `docs/PLAN-leafnumber.md`).**  The leaf number must
encode *track position within the unified work-group*, not the bottom-work ordering-key, whenever a
bottom work maps to more than one recording.  The depth at which a work-group renders must be
*uniform* across its siblings, not per-recording.  Both must refract through "path is a handle, not a
manifest" and yield a stable, playback-sorted, gap-free sequence.  The favoured candidate (per-group
track index) is now strongly preferred over the ordering-key-plus-subsection and
title-carries-uniqueness alternatives because it is the only one that simultaneously fixes Findings 1,
3, and 4.

**Retroactive-maintenance obligation.**  Any change to path construction here re-paths works that are
*already annotated on disk* (the four examples above and every similar split-work / non-uniform-depth
case in the library).  The fix is therefore inseparable from a maintenance-mode re-path pass — the
new path policy must be applied retroactively, journalled, and tag-adjudicated.  The `repath`
subcommand (L4, commit `f1ab378`) stands alone on the `_pipeline`/`_pipeline_io` primitives
(`read_journal`, `_read_tags_*`, `_sha256_file`, `build_dest_path`, `_assess_collisions`,
`_verify_copy`); the `audit`/regroup machinery referenced in earlier drafts of this note is a
separate, not-yet-built sibling plan.

**Meta-lesson (CAPTURE-CANDIDATE).**  On-disk naming artifacts may be the fossil of superseded code.
Before designing a fix from output, verify the *current* code reproduces the artifact — here, two of
the three originally-diagnosed symptoms were already fixed by a prior commit, and re-diagnosing them
would have wasted a fix session on dead code.

## Tree-to-path rendering: two durable rules (from the deferred L2 depth design)

The L2 hierarchy-depth-normalisation design (converged at the 2026 Opus-inflection HALT, then deferred
to a maintenance position — see `docs/PLAN-leafnumber.md`) produced two findings that outlive this
codebase.  Both concern projecting a work's MB *work-tree* onto a filesystem *path*.

**Rule 1 — ragged depth has two sources demanding opposite fixes.**  When sibling movements of one
work render at different directory depths, the cause is one of two things, and they route to different
layers:
- A **data-quality gap** — an MB work is *missing* structure it should have (e.g. a movement whose
  work record has no `part of` link, so it resolves as a standalone top work).  Fix this *upstream*
  (`_works.py` resolution / MB submit-mode) and keep the defect *visible* in the path until then.
- **Faithful non-uniform granularity** — MB *correctly* models some movements more finely than others
  (Handel's IIIa/IIIb; Bach's lettered recits).  This is not a defect; the *renderer* is the right
  layer to *down-project* it onto a uniform path.
Conflating the two sends the fix to the wrong layer — you either bury a real data bug inside a
rendering heuristic, or you "fix" correct data in the renderer when it should have been left visible.

**Rule 2 — clamp-down and pad-up are not symmetric.**  Strict uniform path-depth is unachievable
without inventing phantom directories or destroying real structure.  The achievable universal rule is
*uniform-ceiling, ragged-floor*: render each leaf at ``min(its own tree depth, the group's modal tree
depth)``.  This **clamps over-resolved branches down** (removing structure the path doesn't need —
faithful) but **never pads shallow branches up** (which would invent structure that isn't there —
unfaithful).  The asymmetry is the whole point: a genuinely-top-level node (an opera overture that is
not "part of" any act) *should* sit shallower than its siblings — that raggedness is real and must be
preserved; only the over-resolution (sub-parts deeper than the typical movement) is collapsed.  The
naive goal "make every sibling the same depth" is wrong because it treats the two directions as
interchangeable when one is faithful and the other fabricates.

## Classical Extras as editorial anchor

Every editorial decision on attribution, annotation, and path construction must refract
through the Classical Extras conventions
(github.com/metabrainz/picard-plugins/tree/2.0/plugins/classical_extras).  CE is not just
a tag-format spec — it encodes a coherent stance on how art-music recordings should be
described.  When a new rule (path-vs-tag split, soloist promotion, subgroup demotion) is
proposed, the validation step is: does CE's framing of the same question agree, and if
not, what's the principled reason for diverging?  The default is to follow CE; divergences
need a documented rationale.
