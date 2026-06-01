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

## Cross-medium work-group aggregation (the multimedium substrate)

`run()` processes one medium's *copy* at a time, but aggregates *path/tag* metadata across **all**
media of the release.  The structural fact underneath: a concerto split across two discs, a symphony
whose movements straddle a disc boundary, and a finisher credited on only the last disc were all
silently mis-pathed when aggregation was single-medium.

The substrate that fixes it: `tags_map` is keyed by a single global index over `all_media_pairs`
(every track on every medium, in medium-then-track order).  `top_work_groups` and the three
work-level unification passes (composer, `recording_date_work`, `recording_first_release_date`)
iterate the full map, so a work whose movements straddle a disc boundary is treated as one group for
free.  The copy/tag/verify/journal loop still operates on a `copy_subset` (the selected medium only),
so exactly one medium's files are actioned per `run()` and single-medium copy semantics are preserved
verbatim.  `CopyPlanEntry.idx` carries the global index so `tags_map[idx]` resolves, while
`build_dest_path`'s `global_track_idx` stays copy-subset-local (preserving per-run unique filenames
for the actioned medium).

The durable rule: **aggregation spans media; mutation does not.**  Any future path/tag dimension that
can vary across the movements of one work belongs in the all-media aggregation; anything that copies,
writes, or moves bytes stays scoped to the one medium being actioned.  (Contract C-S0; the eager
all-media MB fetch cost lives entirely in the ingest path — the maintenance/regroup path never
fetches.)

## Concerto-soloist path promotion accumulates across media

The soloist enters the directory path only when it is part of the work's *canonical identity* — the
CE-sanctioned exception to "path is a handle, not a manifest" (above).  The only mechanical signal in
scope is `top_work.type == "Concerto"` (carried into tags as `CWP_WORKTYPE_GENRES_TOP`, because a
concerto *movement*'s bottom-work type is empty — only the root work carries `"Concerto"`).
Symphony-with-soloist, organ symphonies, and other canonical-feature works are an editorial allowlist
deferred to a follow-on.

The promoted soloist set is the **cross-medium union** of the work-group's soloists, carried in a
path-only helper (`cea_album_soloists_unified`, computed by a union pass over the C-S0 work-groups and
*never* written as a file tag).  So a multi-disc concerto whose movements feature different soloists
on different discs accumulates *all* of them into one agreed directory path.

This is the concrete instance of a general editorial rule — **unified path components accumulate per
work across media.**  When tracks and work-hierarchies from multiple media merge into one unified
library path, the path components are cumulative: both a primary composer and a finisher credited on
only one disc accumulate into the unified path; different soloists across discs accumulate.  (The
per-track *tag* worldview need not yet carry the union — that is a separate later initiative; only the
path-construction helpers accumulate.)  Contracts C-S4 and C-S0; refracts P1.

## The `regrouped` journal obligation (closing the detect→adjudicate→act cycle)

Release fragmentation — one release's tracks scattered across multiple work directories, or one work
directory populated from multiple releases — is handled as a three-step cycle that operationalises
"journal detects, tag adjudicates" (above):

1. **Detect (journal).**  The read-only `audit` subcommand groups `action == "tagged"` journal
   entries by `release_id` and by `work_dir` (the second path component) and surfaces the two
   fragmentation shapes.  Cheap: one journal parse, no tag scan, no network.
2. **Adjudicate (tag).**  For each candidate, `audit` reads the embedded `MUSICBRAINZ_ALBUMID` tag
   back from the candidate's destination files and compares it to the journal's `release_id` — a
   candidate is *confirmed* when at least one backing file's present-state tag matches the journal's
   claim, distinguishing real fragmentation from journal staleness.
3. **Act (move + re-journal).**  The `regroup` subcommand moves *confirmed* split-release files to
   their canonical paths (recomputed from embedded tags via `build_dest_path`, offline) and appends
   an `action="regrouped"` journal entry per move.

The obligation: **every regroup move re-journals, or the detector decays with use** (the P2 closing
corollary).  Unlike `repath` (which uses `release_id=""` because it is purely offline), a
`"regrouped"` entry *populates* `release_id` with the MBID that drove candidate selection, keeping the
entry self-describing so a later `audit` can re-confirm it without a MusicBrainz lookup.  The move
preserves the journal-provenance chain verbatim — source SHA captured before the move, destination
SHA verified equal, `_verify_copy` tag round-trip confirmed, and **only then** the journal entry
appended (a crash leaves a complete audit trail).  `regroup` prompts for confirmation before moving
(with `-y/--yes` to skip and `--dry-run` to preview), aligning it with `prune`'s careful posture
rather than `repath`'s act-by-default one.  (Contract C-S8; refracts P2.)

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
`_verify_copy`); the `audit`/`regroup` machinery referenced in earlier drafts of this note **has now
been built** by the multimedium plan's sub-track C (contract C-S8) — see "The `regrouped` journal
obligation" above.  `regroup` reuses the same `repath` provenance primitives.

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

## Codebase audit — handoff brief (post-multimedium)

The multimedium featureset (cross-medium paths + concerto-soloist promotion + the release-fragmentation
audit/regroup cycle) is the explicit hand-off point into the **user-flagged imperative Codebase
audit**.  As the project has grown, do a thorough review of principles, structure, and goals before
the next featureset lands.  Concrete items this featureset surfaced:

- **Deferred `ReleaseContext` / `WorkGroup` aggregation object — decide now whether it is warranted.**
  S0 deliberately did *not* introduce a first-class aggregation object; it threaded the cross-medium
  grouping through `tags_map` (global-indexed) + `top_work_groups` + ad-hoc unification passes in
  `run()`.  That was the smallest correct change, but `run()`'s top-work-group loop now carries five
  passes (leaf index, intermediate sibling index, composer unification, recording-date unification,
  first-release-date normalisation, soloist union) all iterating the same `group_idxs`.  The audit
  should decide whether to lift these into a `WorkGroup`/`ReleaseContext` object — the repeated
  `for grp_idx in group_idxs:` scaffolding is the signal that the abstraction may now pay for itself.

- **`__init__.py` API-surface coherence.**  The thin re-export layer has grown a large `_reexports`
  tuple of *private* helpers (`_assess_collisions`, `_journal_fragmentation_groups`,
  `_confirm_fragmentation`, `_read_albumid_tag`, …) that exist only to bind names for test patching
  (per the "patch where bound" rule).  Audit whether this is the right mechanism or whether the test
  suite should patch the submodule directly, and whether the public `__all__` still reads as a
  coherent API surface distinct from the test-patching re-exports.

- **Maintenance-command confirmation consistency (`repath` gap).**  `prune`, `apply --delete`, and the
  new `regroup` all confirm before destructive action (`confirm`/`--yes`); `repath` mass-relocates the
  whole library with no prompt (only `--dry-run`).  The most destructive command is the least guarded.
  Consider a single shared confirmation helper for all library-mutating maintenance commands.  (Also
  recorded durably in `docs/BACKLOG.md` so it survives this note's eventual absorption.)

- **Module-boundary review.**  `_pipeline.py` now hosts three top-level maintenance entry points
  (`run`, `repath`, `regroup`) that share a move/verify/journal provenance loop near-verbatim three
  times.  Evaluate factoring the shared "move one file with SHA + `_verify_copy` + journal" primitive,
  and whether the maintenance commands belong in their own module distinct from the ingest pipeline.

- **Concerto-soloist editorial allowlist (follow-on to the mechanical case).**  Only
  `top_work.type == "Concerto"` ships; organ symphonies, violin-feature works, and symphony-with-soloist
  are canonical-identity but not type-`Concerto`.  The audit should decide whether the allowlist /
  "solo X" instrument-relation signal is in scope or stays deferred.
