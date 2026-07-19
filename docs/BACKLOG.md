# music-annotator — Backlog

Cross-cutting items that are not part of any active sharded plan and have no committed substrate yet.
Each is preserved in full so no design context is lost; when one acquires a substrate and a clear
decomposition it graduates into its own `PLAN-*.md` (or a sub-track of an existing one) and is
removed from here.  All prior plans are retired (2026-07).

**Roadmap partition (2026-07-18).**  The library-completion arc (Acts I + II + III-a) graduated to
`docs/ROADMAP.md` as one sub-track DAG (nodes R0–R6, junctures J1–J3).  Graduated items below carry a
`→ ROADMAP Rn` marker; their full design context **stays here** until the node's `PLAN-*.md` is
derived, then tombstones.  The playlist library graduates to its own ROADMAP when Act I nears
completion.  Everything unmarked (Act III-b, MB-upstream, editorial, mbngs2, trigger-based items)
remains backlog-resident by design — operator-, research-, or trigger-paced rather than
session-sequenceable.

Organised by the **phase arc** (north star, then Acts I–III on the critical path, then parallel
tracks).  Each item keeps its provenance line; graduation tombstones and execution learnings stay at
the bottom.

---

## North star

Three commitments (user, 2026-07-17) shape every item below:

- **Full inclusion — the library is a catalog.**  Like a Library of Congress catalog system, the
  library directory admits everything: irrespective of context, scope, age, or style, there either
  is or will be created a taxonomic path for each medium/dir.  Nothing stays outside in `Original/`
  ("jowels and cheeks together" — non-classical, audiobooks, family recordings included).
- **Coverage before quality.**  The library does not have to be *complete* in the sense of perfectly
  annotated — but nothing is left out.  Every source dir is integrated at the best rung currently
  achievable (full MB annotation → MB-partial → alternate-source (Discogs, …) → source-tags-only
  provisional), with provisionality explicitly marked.  Getting everything in at least provisional
  form converts the ingest backlog into a maintenance workload — the shape the existing idempotent
  machinery (`audit --enrich`, `repath`, `regroup`) was built for — and means every future revision
  pass, census, and audit operates over one complete corpus instead of a moving in/out boundary.
- **Two lenses: filesystem = catalog, playlists = reading room.**  The filesystem taxonomy stores by
  work structure ("path is a handle, not a manifest" — `docs/NOTES.md`).  The **playlist library**
  is the main valuable, intuitive access lens: it reconstitutes the intentional wholes the taxonomy
  deliberately decomposes — "albums" in the legitimate sense (a track sequence that, though perhaps
  heterogeneous and of separate works, nevertheless comprises an intentional whole, or whose
  sequence and progression is itself an artistic statement); entire multi-movement and multi-part
  works, freed from the antiquated partitions of storage media; cycles of works; box-set collections
  (a composer's complete works, a performer's complete recordings).  Album/sequence identity
  migrates from directory structure to playlist objects.

**Sequencing spine:** Act I (full inclusion) and Act II (naming-policy freeze) jointly gate
Act III-a (the one-pass naming re-derivation).  Act III-b (the provisional-upgrade loop) and the
parallel tracks (playlist lens, MB-upstream, editorial, infrastructure) are ungated and ongoing.

The layer-routing rule (generalising `docs/NOTES.md` "ragged depth has two sources"): **a defect is
fixed at the layer that owns it and kept visible until then.**  Naming/layout defects (issue class A)
→ the renderer/policy layer (Act II / III-a).  Wrong or conflicting MB data (class B) → the
MB-upstream track, never compensated in the renderer.  Information wrong at the source — reception
history, mistitled works, pre-MB errors (class C) → the editorial/scholarly track.

---

## Act I — Full inclusion (critical path)

Goal: drain `Original/` — every dir integrated into the annotated library at its best achievable
rung.

### Census of `Original/` (first step)  → ROADMAP R0

~218 top-level dirs remained (2026-07-17).  **Pre-step COMPLETE (2026-07-18): the user manually
pruned `Original/` of non-library material** — the census is unblocked and is Act I's next action.
Classify each remaining dir into: Bach Edition remainder (existing
pipeline handles) / PrestoMusic download / whipper rip / not-in-MB / track-mismatch /
non-classical–other (Amazon Music, Audiobooks, Dance, Education, …).  Same census-before-policy
pattern as the L2 depth census.  The distribution prioritises the rest of Act I: it decides whether
the Presto adapter, the whipper adapter, or the provisional rung is the binding constraint, and it
inventories the non-classical corpus the taxonomy must admit (feeds the Act II taxonomy design).

### Bach Edition remainder (operational)  → ROADMAP R5

Some Brilliant Classics Bach Edition media dirs remain and are handleable by the existing pipeline —
run them through on hades.  No dev work; listed here only so Act I's exit condition ("`Original/`
empty") accounts for them.

### Provisional-ingest mode — the rung ladder (anchor item)  → ROADMAP R2

New load-bearing design item (2026-07-17).  Ingest any source dir at the best achievable rung and
record the rung so upgrades are enumerable.

- **Rung ladder (provisional sketch):** full MB annotation → MB-partial (track mismatch tolerated) →
  alternate-source (Discogs, …) → source-tags-only minimal.  Exact rungs decided at plan time.
- **Marking substrate.**  Provisionality must live in the track+sidecars unit itself (tag or sidecar
  — present-state authority), journal as detector, per `docs/NOTES.md`
  "database-as-infrastructure".  Something like an annotation-rung / source-provenance tag so
  `audit` enumerates provisional entries cheaply and an enrich-style pass finds upgrade candidates.
- **Lossless-principle consistency.**  "Never silently degrade" (AGENTS.md invariant) permits
  *deliberate, explicit* degradation only if it is persisted as a first-class fact — the same
  failure-vs-no-data discrimination the `_net` item enforces at retrieval time, but persisted.
  Unmarked provisional entries would violate the invariant's spirit.
- **Path construction without a work tree.**  The naming heuristics derive from the MB work
  hierarchy; Discogs has essentially no classical work structure and not-in-MB releases have none.
  Provisional paths will be flat and will churn on upgrade — a known, accepted cost; `repath` +
  journal provenance (C-PROV/C-MOVE) already handle exactly that.
- **Upgrade path** is Act III-b (the perpetual loop); this item builds the substrate it consumes.

### Source-adapter support (new ingest provenances)  → ROADMAP R3a / R3b

- **PrestoMusic downloads.**  Source directories containing tracks downloaded from PrestoMusic.  These
  dirs may contain their own cover art and booklet, but music-annotator should still query
  MusicBrainz for a tag comparison and enrichment.  (PrestoMusic files always carry ISRCs — see
  `PLAN-fingerprint.md` F2 (retired), which activated ISRC as an identity rung.)
- **whipper and MakeMKV.**  Source-adapter support for these rippers.  whipper additionally produces
  an **AccurateRip** result (rip-fidelity against a crowd consensus of the same pressing) and a
  proper MB disc-ID from its TOC — both high-value provenance signals.  AccurateRip is the intended
  **4th archival dimension** reserved by the fingerprint plan (see "`accuraterip` 4th archival
  dimension" under Infrastructure): this whipper ingest mode is the source-adapter work that
  produces/exposes it.  When sharded, this becomes a source-adapters plan that the rung-0 source-tag
  reader consumes.

### Alternate metadata source: Discogs adapter  → ROADMAP R3c

New (2026-07-17).  Where MB coverage fails (not-in-MB releases, or MB data too wrong to use), Discogs
or another source may provide enough tag coverage to be adequate for a mid-ladder rung.  Design
questions at plan time: identity mapping (Discogs release ID ↔ tags — a `DISCOGS_RELEASE_ID`-style
tag as the present-state key, analogous to `MUSICBRAINZ_ALBUMID`), which tag fields Discogs can
populate, and the explicit rung marking (Discogs-sourced entries are inherently provisional with
respect to work-tree-derived paths, since Discogs lacks classical work structure).

### Track-mismatch-tolerant ingest  → ROADMAP R3d

New (2026-07-17).  Several remaining dirs mismatch MB's track records (extra/missing/reordered
tracks, or a different edition).  Per-release adjudication: (a) the MB data is wrong → fix upstream
(MB-upstream track) then ingest at full rung; (b) the source is a genuinely different edition → find
or create the right MB release; (c) neither is economical now → ingest at a partial/provisional rung
with the mismatch recorded.  Needs a pipeline mode that tolerates a declared mismatch instead of
refusing, without weakening the default strict posture.

[jfindlay edit: The policy should be the local library is as accurate as MB and/or Discogs info
admits.  We can correct errors in either if evidence supports, like info on the physical backing
media.  Perhaps a lesser celebrated aspect of scholarship is that it always remains incomplete.  It
is rare for a field, or even a result to be complete.  In research, an answer, even a conclusive
answer, invariably uncovers more questions.  This is the humility of the scholar: to always aim for
excellence and always accept inevitable error.  The study of composition and recording are not
exempt and the history and artifacts of both are irrecoverably incomplete and even contradictory.
Unless there is like a `Note` tag convention, I'm not sure how to annotate the annotations when an
omission or error is known (an annotation on the annotations), kind of like a quantification of
error, or an error bar.  The tags are not the place for scholarship, at least not beyond the
curation and biblioteconomy of the local library itself and for MB/Discogs.  A good library is
optimally organized under the constraints.  Some of these constraints are errors and omissions in
the music and recording libraries of the world, others are from contributors to MB/Discogs, still
others are ours.  A good engineer finds adequately optimal balance among all types of detractions
and constraints.  Yet, as the fount of enthusiasm continues along the worldline, the work improves
and converges further still.]

### Not-in-MB routing rule  → ROADMAP R3e

For each not-in-MB release, choose: (a) create the MB release upstream (→ MB-upstream track —
enriches the commons; full-rung ingest afterwards); (b) Discogs-sourced rung; (c) source-tags-only
provisional rung.  Default posture decided at census review; likely per-release judgment with (a)
preferred where the release is close to MB-ready (physical media in hand → scans/disc-IDs can back
the MB edit; see also "Submit disc IDs to MusicBrainz" in the MB-upstream track).

---

## Act II — Naming-policy convergence

The open naming-policy questions (issue class A).  Each must close before Act III-a runs, because the
revision pass re-derives the whole library under the *final* heuristic.  Not gated on Act I; can
proceed in parallel.

### Depth emergence (issue A-a) — routing note, mostly closed  → ROADMAP R6a (execution) + MB-upstream

"Path depth should emerge from the actual musical work structure" **is** the converged L2 design:
uniform-ceiling / ragged-floor renders depth emergent — clamp over-resolution down, preserve real
shallowness, never pad up (`docs/NOTES.md` "Tree-to-path rendering: two durable rules").  The
remaining half of A-a — "incorrect or unusual MB data leading to bizarre filetree layouts" — routes
per Rule 1 to the **MB-upstream track**: fix the data at its layer; keep the defect visible in the
tree until fixed; never compensate in the renderer.  No new renderer design is needed for A-a; the
open work is Act III-a execution (W3b/L2 below) plus B-track data fixes as the census surfaces them.

### Library-wide taxonomy + initial directory component (issue A-b, expanded)  → ROADMAP R4a

The original A-b concern: composer / performer / conductor / ensemble initial-directory-component
decisions and related issues.  Full inclusion (north star) widens it: the top-level taxonomy must now
admit non-classical and non-music content (jazz, popular, audiobooks, spoken word, family recordings,
education, …) LoC-style — a class for everything.  Two design layers when this graduates:

1. **Top-level class scheme** — what the first path component(s) of the whole library look like when
   classical art-music is one class among several.
2. **Within-classical initial component** — the existing composer-first convention and its edge
   cases (recitals, compilations, performer-led releases); absorbs the path implications of the
   native-language/script item (editorial track) and refracts through "path is a handle, not a
   manifest" and the CE editorial anchor.

### Cross-medium fragmentation inventory (issue A-c)  → ROADMAP R4b

Cross-medium MB attributions/annotations still fragment the filetree in some cases despite the C-S0
all-media aggregation substrate and the audit/regroup detect→adjudicate→act cycle (`docs/NOTES.md`).
Inventory what still fragments — box sets MB models as multiple releases, per-medium artist-credit
differences, release-vs-release-group attribution splits — before designing anything; the remedy may
be mostly B-track (MB data corrections) or III-b (regroup passes) once enumerated.

### Concerto-like soloist override — editorial allowlist (follow-on to multimedium S5)  → ROADMAP R4c

The mechanical `top_work.type == "Concerto"` case shipped in `PLAN-multimedium.md` S5 (soloist
promoted into the path, accumulated across media — see `docs/NOTES.md` "Concerto-soloist path
promotion accumulates across media").  The remaining open item is the **non-mechanical
canonical-soloist works**: Saint-Saëns Symphony no. 3 (organ), "Cinema Serenade" (violin), and
symphony-with-soloist generally — canonical-identity but *not* MB type-`Concerto`.

Candidate signals: a "solo X" instrument-relation type on the recording/work, dedicated work-title
patterns, or an editorial allowlist.  The rule answers *"is the soloist part of the work's canonical
identity?"* — not *"is the soloist on the release?"*.  All decisions refract through the Classical
Extras path-vs-tag distinction (primary attribution in path, full credits in tags; see `docs/NOTES.md`
"Path is a handle, not a manifest").  Substrate is already in place (C-S4 `CWP_WORKTYPE_GENRES_TOP`,
the C-S0 cross-medium soloist union, and the `build_dest_path` concerto-injection site), so this is a
small additive session once the editorial signal is decided.

---

## Act III — Library revision

### III-a — One-pass naming re-derivation (gated on Act I coverage + Act II freeze)

One pass that re-derives the **whole** library under the final heuristics — making the library "more
like itself" — rather than piecemeal retro-fixes (user, 2026-07-10: cross-version consistency
deferred to this phase rather than chasing backward-compat incrementally, which would be its own
edge-case accretion).  Absorbs the items below plus any other accumulated cross-version naming drift.

#### Catalogue-colon part-label retro-fix + library-revision reconciliation  → ROADMAP R6b

**Heuristic fix shipped** (`strip_common_prefix` in `_works.py`); this item is the **deferred retro-fix**
of already-annotated releases.

**Root cause (fixed forward).**  `strip_common_prefix`'s colon-fallback split on the *first bare
`":"`* to separate a `Title: Movement` label.  MusicBrainz work titles embed a colon *inside*
catalogue numbers — Haydn Hoboken (`"…, Hob. III:31"`), Bach chorale subtitles
(`'"Fantasia super: Komm heiliger Geist…"'`), Handel double-colon (`"HWV 350: 16: (Minuet)"`).  When
such a title reached the colon-fallback (child does **not** share the parent-work prefix) and the
catalogue colon was the *only* colon, the split produced a bare number as the part label.  Confirmed
symptom in the library: `Haydn - The Angeles String Quartet/String Quartets, op. 20 [rel 2000]/`
minted intermediate directories `01 - 31`, `02 - 32`, `03 - 33` (the Hoboken numbers III:31/32/33),
and the same corruption in `CWP_GROUPHEADING` (`"String Quartets, op. 20 :: 31 :: I. Allegro moderato"`).

**The forward fix** (already applied): the colon-fallback now keys on `": "` (colon **followed by
whitespace**), not a bare `":"`.  The discriminator is structural, not per-composer: the CE
`Title: Movement` separator is always written with a trailing space; a catalogue colon is flanked by
non-space characters.  This is *more* general than the old code, not more convoluted — it correctly
handles Hoboken/BWV/HWV without any catalogue table.  Tests added in `test_annotator.py`
(`TestStripCommonPrefix`): catalogue-colon-no-split, colon-space-still-splits, first-`": "`-wins.

**Deferred: retro-fix of already-processed releases.**  The forward fix stops *new* `NN - NN`
directories during ongoing annotation but does **not** touch releases already on disk.  A `repath`
pass (re-derive all paths + re-patch `CWP_PART_*` / `CWP_GROUPHEADING` tags under the final
heuristic) belongs to this phase — do not disrupt an in-progress library with piecemeal renames.

**Scope when reopened**: survey the full library for `NN - NN` intermediate dirs and for any
`CWP_PART_*` value that is a bare catalogue fragment; `repath` the affected releases; re-patch tags.
Survey at fix time found the bug had *fired* on 1 release, but the latent catalogue-colon pattern is
present in ~16 Haydn releases and in Bach/Handel titles — the census must be re-run against the
then-current library, not assumed to be the single Angeles release.

#### Hierarchy-depth normalisation — W3b (deferred from PLAN-naming.md)  → ROADMAP R6a

**Deferred from `docs/PLAN-naming.md` W3b** (2026-06-02, plan retired).  Dedicated multisession
planned.

**Session scope (from PLAN-naming.md W3b)**:
- Add modal-depth computation over a `top_work_groups` group to `_pipeline.py`'s work-group loop.
- Extend `build_dest_path` to accept (or compute internally) the group modal depth and apply the
  uniform-ceiling clamp.
- Extend `repath` to pass the group context so the clamp is applied during retroactive re-pathing.
- `repath` the 35 affected work_dirs.
- Freezes **C-W3b** (the depth-normalisation rule in `build_dest_path`).
- Tests: 100% branch coverage.  Ragged-floor case (preserve); over-resolution case (clamp); the
  W3b change does not affect the W3a-corrected files (no regression on leaf-collision / `dd.dd`
  paths).

**Files expected**: `src/music_annotator/_tags.py` (`build_dest_path`),
`src/music_annotator/_pipeline.py` (work-group loop + repath group context),
`tests/unit/test_pipeline.py`, `tests/unit/test_annotator.py`.

**Juncture required before sharding**: the depth-clamp implementation in `build_dest_path` is an
architectural boundary decision — it changes the path output for 35 work_dirs (~3% of the library)
and becomes the permanent policy for all future `run()` annotations.  The juncture review must
confirm:
- The exact rule (uniform-ceiling / ragged-floor per NOTES) and how it is expressed in
  `build_dest_path`'s interface.
- The backward-compat approach (`depth_clamp` parameter vs. always-on vs. opt-in).
- Whether the two sub-shapes (ragged-floor faithful vs. over-resolution clamp) can be distinguished
  from available tag data alone (`CWP_PART_LEVELS`, group modal depth) or require a MB network call.

**C-W3b contract (provisional)**:
- **Uniform-ceiling / ragged-floor**: render each leaf at `min(its own tree depth, the group's modal
  tree depth)` (NOTES "Tree-to-path rendering: two durable rules").  Clamp over-resolution *down*;
  never pad shallow branches *up*.
- **Two sub-shape routing**: a genuinely-shallower node (ragged-floor, e.g. a standalone overture
  with no `part-of` link) is left at its own depth.  A sub-part deeper than the modal depth
  (over-resolution, e.g. Handel IIIa/IIIb) is clamped down to the modal depth.
- **Distinguishing the two**: a node whose shallowness is caused by a *missing* `part-of` link
  (data-quality gap) is kept shallow and visible; the defect must be surfaced upstream.  A node that
  is faithfully more granular than its siblings is clamped.  The distinction is `CWP_PART_LEVELS`
  vs expected depth from the group's modal `CWP_PART_LEVELS`.
- **Backward-compatible**: `build_dest_path` gains a `depth_clamp` parameter defaulting to `None`
  (current behaviour) until W3b's `repath` pass completes; then the default flips to the modal
  depth.  Existing callers (`run`, `repath`, `regroup`) pass the group context needed to compute the
  modal depth.

#### Hierarchy-depth normalisation (deferred L2 of the leaf-numbering plan)  → ROADMAP R6a

The leaf/intermediate numbering fix (L0/L1 of the now-complete `PLAN-leafnumber.md`) shipped; the
**depth-uniformity** half (L2) was designed at an Opus-inflection HALT and then **deferred** — the
user elected not to ship depth normalisation until the library is complete and the full distribution
of depth shapes is known (designing from a maintenance position rather than the 36-group census).
Under the north star, "complete" = Act I full inclusion.  The converged design is preserved as two
durable rules in `docs/NOTES.md` ("Tree-to-path rendering: two durable rules") — ragged depth has two
opposite-routing sources, and the *uniform-ceiling / ragged-floor* rule (render each leaf at
`min(own tree depth, group modal depth)`: clamp over-resolution down, never pad under-resolution up).
When reopened it materialises as an additive pipeline pass writing `cwp_render_levels` as
model_extra, consumed by `build_dest_path`'s depth branch, falling back to raw `cwp_part_levels` when
absent.

Scope when reopened (from the census of 36 non-uniform groups in 6 shapes):
- **Shape A (20 groups) — out of scope, preserve.**  Overture/sinfonia at PL=1 among PL=2 acts is
  genuinely top-level (ragged *floor*); the rule must not over-normalise it.
- **Shapes C/D (3 groups: Handel Water Music, Bach Matthäus-Passion, Haydn Schöpfung) — the target.**
  A movement has MB sub-parts (IIIa/IIIb; lettered recits) nesting deeper than flat siblings; clamp
  the over-resolution down.
- **Shape B (9 groups, mixed flat/split movements)** and **Shape F (2 groups, excerpt discs, depth
  spread {1,3})** — per-shape call deferred to reopen (likely acceptable as-is / near-arbitrary modal).
- Pinned corner cases: modal ties → shallower depth; PL=0 orphans (Shape E) excluded from the modal
  computation (see "PL=0 orphan tracks" in the MB-upstream track).
- **Reopen criteria:** when the library is complete (Act I done — more depth shapes likely), or
  sooner if a new shape appears that the uniform-ceiling rule mishandles.

##### Non-uniform-depth census (library scan)

Full scan of `~/Remote/hades/Music/Done/` at the time of the L2 design — **3663 FLACs, 0 MP3, 1006
work-groups** (a work-group = all tracks of one release sharing a `CWP_WORKID_TOP`).  A group is
*non-uniform* when its tracks carry differing `CWP_PART_LEVELS`.  **36 groups (3.6%)** were
non-uniform, in six shapes.  Re-run the scan when L2 reopens to refresh against a more complete
library: `scripts/scan_nonuniform_depth.py` (depends only on `mutagen`; adjust its `ROOT`).

| Shape | n | What it is | Correct? | L2 treatment |
|-------|---|------------|----------|--------------|
| **A** | 20 | Overture/sinfonia/epilogue at PL=1 among PL=2 acts/numbers (Die Meistersinger Vorspiel, Così Ouverture, Nutcracker Ouverture ×3, Verdi Requiem Offertory, Missa solemnis Agnus Dei) | **YES — overture genuinely sits at top of the opera** | **Out of scope — preserve. Must not over-normalise.** |
| **B** | 9 | Mixed flat/split movements: some movements single-track (PL=1), others split into sub-movements (PL=2) (Mozart Missa c-Moll, Requiem K.626, Verdi Requiem, Mendelssohn *Lobgesang*, four Grumiaux violin sonatas, Divertimento K.287) | Arguably correct | Decide at reopen (likely acceptable as-is) |
| **C** | 1 | Suite with one multi-part movement (Handel Water Music — Suite 1 movt III has sub-parts IIIa/IIIb → PL=3 among PL=2) | **NO — ragged depth** | **Primary target** |
| **D** | 2 | Oratorio with multi-part numbers (Bach Matthäus-Passion: 14 PL=3 tracks from lettered recits; Haydn *Schöpfung*: Nr.18/19 → XIXa/b) | **NO — ragged depth** | **Primary target** |
| **E** | 2 | PL=0 orphan: a movement's MB work has no `part of` link → resolved as standalone top work (Mozart Divertimento K.136 "II. Andante"; Litaniae K.243 "X. Miserere") | **NO — different bug** | **Out of scope → MB-upstream track** |
| **F** | 2 | Highlights disc with depth-mismatched excerpts (Tannhäuser: Overtüre PL=1 vs Bacchanale PL=3; Tristan: Vorspiel PL=2 vs Liebestod PL=3) | Edge case | Defer / decide at reopen |

**Extreme case:** Tannhäuser highlights — depth spread of 2 (PL={1,3}) in a 2-track group; the only
true spread-≥2 case among non-zero depths.

**The bigger, orthogonal signal — multi-recording-per-bottom-work (16 groups).**  Independently of
depth, 16 groups had at least one bottom work (`CWP_WORKID_0`) holding >1 recording — the *direct*
driver of the leaf-collision bug that L0 fixed.  Only **3** of these 16 overlapped the
non-uniform-depth set (Handel, Così, Die Meistersinger — the last has 12 bottom-works holding >1 rec,
max 10, the worst leaf-collision in the library).  The other **13 were uniform-depth** (Mahler 9 — 4
bottom-works ×up to 8 recs; Boccherini *Musica notturna* ×5; Sibelius Symphony 7 ×4; …).  This is why
L0/L1 (per-group leaf index) was the load-bearing fix — it covers all 16 multi-rec groups regardless
of depth — and L2 (depth) is the smaller, secondary concern touching only Shapes C/D (3 groups).
Do not let L2's intricacy inflate its priority.

#### AcoustID tag naming + semantics — Picard alignment  → ROADMAP R6c

**Deferred by user (2026-07-14)** until library annotation is (mostly) complete; a persisted-tag
migration — do not churn the convention mid-flight.

**The confirmed inconsistency.**  The `ACOUSTID_ID` tag is written with **two semantically
different UUIDs** depending on which path touches the file:
- **Main pipeline** (`_pipeline.py` → `fetch_acoustid_id`): the AcoustID **track UUID** from
  `/v2/track/list_by_mbid` (recording-MBID keyed).
- **`enrich(re_resolve=True)`** (`_pipeline_maint.py` → `_fetch_acoustid_lookup_raw`): the
  AcoustID **cluster UUID** from `/v2/lookup` `results[0].id` (fingerprint keyed).
These can be different UUIDs for the same file; the audit pass (`_audit.py`, journal-vs-tag
`acoustid_id` compare) could flag spurious mismatches on a file touched by both.

**Authoritative reference (Picard, per AGENTS.md tag-convention anchor).**  Confirmed against the
MetaBrainz community thread with Picard's lead dev (Philipp Wolfer / `outsidecontext`,
`community.metabrainz.org/t/acoustid-id-vs-acoustid-fingerprint/676749`).  Picard defines exactly two
AcoustID tags:
- **`acoustid_id`** = *"the ID returned as a result for the fingerprint lookup on acoustid.org"* —
  i.e. the **cluster UUID from `/v2/lookup`**.  This is what `enrich` writes; the **main pipeline's
  `list_by_mbid` track UUID is the divergent-from-Picard value.**  (Correction to an earlier
  session framing that called the enrich write "the bug" — against Picard it is the pipeline that
  diverges.)
- **`acoustid_fingerprint`** = the raw **Chromaprint fingerprint string** (not a UUID; recomputable
  from audio; Picard does not write it by default).  music-annotator already stores this value but
  **under the key `CHROMAPRINT_FP`** ("Chromaprint Fingerprint"), not Picard's `ACOUSTID_FINGERPRINT`.

**User intent**: keep **both** AcoustID values in the tags — the AcoustID UUID and the fingerprint.
Both are already stored; the work is making them *consistent* and *Picard-conformant*.

**Scope when reopened** (decide the two sub-questions at that time):
1. **`ACOUSTID_ID` value**: make it the `/v2/lookup` cluster UUID **everywhere** (Picard-aligned;
   both writer paths source it identically; the main pipeline switches from `list_by_mbid` — note
   this adds an fpcalc + lookup dependency to the pipeline for a value it currently gets cheaply from
   the recording MBID).  *Alternative*: keep both UUIDs as distinct tags (`ACOUSTID_ID`=cluster +
   a new `ACOUSTID_TRACKID`=track UUID) — nothing lost, no new fpcalc dependency, but a non-Picard
   tag extension.
2. **`CHROMAPRINT_FP` → `ACOUSTID_FINGERPRINT` rename** for full Picard conformance — a persisted-key
   migration (existing files carry `CHROMAPRINT_FP`; audit/enrich must read both old and new keys
   during transition).  May not be worth it if `CHROMAPRINT_FP` is an intentional project choice.

### III-b — Perpetual upgrade loop (ungated, ongoing)

The maintenance mode created by Act I's rung ladder: re-resolve provisional entries as better data
appears — MB edits land (ours or others'), Discogs coverage improves, physical media and artwork get
scanned (user, 2026-07-17: scanning is available later where necessary).  Mechanism: extend the
`audit --enrich` / `--re-resolve` posture (P-FP3 idempotent maintenance) to rung upgrades; every
path change journalled via the C-PROV/C-MOVE primitives; every upgrade re-marks the rung tag so the
provisional census stays current.  Unlike III-a this never "completes" — it is the steady state of
an archival library.

---

## Playlist library — the access lens (parallel track)  → future ROADMAP (graduates near Act I completion; decided 2026-07-18)

Graduates the former one-liner ("Playlist generation for collection/cycle groupings (Ring cycle,
symphony cycles, etc.)") into the second lens of the north star.  The filesystem stores by work
taxonomy; playlists reconstitute the intentional wholes that storage decomposes.  Shapes to support:

- **Album playlists** — a release's track sequence as an intentional whole (including heterogeneous
  recital/compilation programmes whose progression is itself the artistic statement).
  Reconstructable from embedded `MUSICBRAINZ_ALBUMID` + disc/track positions — tag-held, so the
  playlist layer is a *derived, regenerable artifact* per database-as-infrastructure.  Human-curated
  sequences are the exception: authored data, not derivable.
- **Whole works and cycles** — multi-movement/multi-part works freed from storage-media partitions;
  work cycles (Ring, symphony cycles); MB work hierarchy + release-group / series relationships as
  the grouping signals.
- **Collections** — box sets, a composer's complete works, a performer's complete recordings.

Design questions when this graduates: playlist format and storage location (in-library `.m3u8`?
dedicated sidecar tree?), regeneration tooling (a `playlists` subcommand consuming tags + journal),
and which MB entities (release-group, series) need fetching and caching.  Benefits from Act I
coverage (playlists over a complete corpus) but is not gated on it.

---

## MB-upstream data track (class B — parallel)

Wrong or conflicting MusicBrainz data is fixed *in MusicBrainz*, never compensated in the renderer;
the defect stays visible in the tree until the fix lands (Rule 1, `docs/NOTES.md`).

### Submit disc IDs to MusicBrainz

When `parse_disc_toc` succeeds (a valid `00 - disc info.yaml` is present) but `_match_medium_by_toc`
finds no registered disc IDs on the release, music-annotator has the FreeDB CRC and sector offsets
needed to compute a proper MusicBrainz disc ID.  A future phase could offer to submit the disc ID to
MB via the `/ws/2/discid` endpoint, permanently enriching the database and enabling TOC-based
selection for all users.  Requires an authenticated MB session; defer until a login/credential flow
is designed.

[jfindlay edit: We also need to attest we're submitting new or corrected data for the correct
release.  At least one of the MB annotations are taken from a parallel release.  Rather than go
without, I used the FR release of the EMI Karajan/BPO Schubert symphonic cycle rather than the JP
release, from which the rips were taken.  Perhaps taking this knowingly misleading action was not
ideal.  The DiscIDs matched, but any updates on that album would be wrong.  The user (me), in some
cases like this or if there is doubt, will have to physically access the backing media to verify
that each release for which changes are submitted match MB/Discogs releases rather than corrupt
those data sources.]

### PL=0 orphan tracks — hierarchy-resolution / MB-data-gap defect

Two groups in the L2 census (Mozart Divertimento K.136 "II. Andante"; Litaniae K.243 "X. Miserere")
have a single movement whose MB work record carries **no `part of` relation**, so
`build_work_hierarchy` (`_works.py`) resolves it as a standalone top work (`CWP_PART_LEVELS=0`,
`workid_0 == workid_top`).  This is a hierarchy-resolution / MB-data-gap defect, **not** a
numbering-policy question — it was explicitly scoped out of the leaf-numbering plan's L2 so it would
not contaminate the depth-rendering policy.  Candidate fixes: a `_works.py` resolution improvement,
an MB submit-mode correction (add the missing `part of` link upstream), or an editorial allowlist.
Per the "ragged depth has two sources" rule in `docs/NOTES.md`, the defect should be kept *visible*
in the path until fixed at the data/resolution layer, never papered over in the renderer.

### Per-release MB corrections surfaced by Act I

The census and the mismatch/not-in-MB routing will produce a running list of releases whose MB data
needs editing (wrong track lists, missing releases, missing `part of` links, wrong attributions —
issue class B generally, and the data half of issue A-a).  Track them here (or in a census artifact)
as they surface; each fixed MB record upgrades a provisional entry via III-b.

### musicbrainzngs2 contributions (external dependency track)

`python-musicbrainzngs` (0.7.1, 2020) is effectively unmaintained — 47 open issues, 16 open PRs, no
releases since 2020.  A fork, `C0rn3j/python-musicbrainzngs2`, began modernisation in January 2026
(Python 3.10+, ruff, pyproject.toml, partial type stubs) but has not addressed the substantive bugs
or gaps music-annotator hit.  Not yet on PyPI.

music-annotator will migrate once musicbrainzngs2 reaches a stable release covering the fixes below.
Until then, local monkey-patches remain in `_mb_api.py` and are removed as each upstream fix lands.

Items are sketched at PR granularity; exact payload size decided as each is started.  Proceed
carefully and require slow human review+styling — we don't know how the maintainers will respond to
high-volume agent-written changes.

#### Bug fixes (directly blocking or affecting music-annotator)

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

#### Modernisation (C0rn3j's stated goals)

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

---

## Editorial / scholarly track (class C — parallel, slowest)

Information wrong at the source — titles imposed by reception history, impresarios, publishers;
errors going back to the recording and composing of the music itself.  Resolved against scholarly
sources (Wikipedia / IMSLP / urtext evidence), not MB alone.

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

## Infrastructure (ungated — parallel)

### Unified network-retrieval subpackage (`_net`) — lossless-archival retrieval policy  → ROADMAP R1

**GRADUATED — R1 sub-track complete (2026-07-19, commits `011668e`–`39dc90f`).**

`_net.py` ships with `RetryDecision` (RETRY/NO_DATA/FATAL), `NetPolicy`, and `retrieve()`.  All MB
data, CAA, and AcoustID fetches route through `retrieve()` with structured classifiers — no
`str(exc)` scraping anywhere on those paths.  `fetch_acoustid_lookup` collapsed into
`_fetch_acoustid_lookup_raw` (callers slice the tuple).  Universal terminal rule applied: AcoustID
retries-exhausted / malformed JSON / transport failure now raises (propagates to the per-release
boundary in `discover()`), closing the lossless-principle gap.  CAA left musicbrainzngs entirely
(direct `urllib` + URL templates).  `_patched_safe_read` surface confirmed MB-data only.

**Remaining gap (follow-on, see next item).**  Two `_discover.py` search/disc-ID calls
(`_search_mb_releases` via `mb.search_releases`; `search_releases_by_dir` via
`mb.get_releases_by_discid`) remain on the legacy `_mb_retry`/`_mb_call` path with a live
`"404" in str(exc)` scrape.  These were never enrolled in R1's session list.  `_mb_retry` and
`_mb_call` survive solely for these two callers.

### Migrate MB search/disc-ID calls onto `_net`; retire `_mb_retry`/`_mb_call`

**Motivation.**  R1 left two `_discover.py` call sites on the legacy `_mb_retry`/`_mb_call` path
(surfaced at the R1 sub-track boundary by `@plan-juncture`, 2026-07-19).  After this item, every
remote fetch in the codebase routes through `_net.retrieve()` with a structured classifier — the
"uniformly on `_net`" claim the R3 adapters lean on holds literally, and the last `str(exc)` scrape
is gone.

**Scope.**
- `_search_mb_releases` (`_discover.py`) — wraps `mb.search_releases` via `@_mb_retry` + `_mb_call`.
  Replace with `retrieve(lambda: mb.search_releases(...), policy)` using `_mb_data_classify` (already
  written in S2; the same classifier covers search errors).
- `search_releases_by_dir` (`_discover.py`) — wraps `mb.get_releases_by_discid` via `@_mb_retry` +
  `_mb_call`; string-scrapes `"404" in str(exc)` to detect no-disc-ID.  Replace with `retrieve(...)`
  using `_mb_data_classify`; the 404 case is already `NO_DATA` in that classifier (returns `None` →
  map to `[]`).
- Delete `_mb_retry` and `_mb_call` from `_mb_api.py` once both callers are migrated.  Update
  `_discover.py`'s import of `_mb_retry`, `_mb_call` accordingly.
- Update `tests/unit/test_discover.py` — `TestSearchReleasesByDir` and `TestSearchMbReleases` patch
  targets and retry/exhaustion assertions must align with the `_net`-backed path.

**Interactions.**
- `_mb_data_classify` (written in S2) already handles `mb.ResponseError` with typed `exc.cause.code`
  and plain `OSError` transport failures — no new classifier needed; just wire the two call sites.
- `_patched_safe_read` remains (MB-data only; removal gated on mbngs2-1).
- After this item, musicbrainzngs's role in music-annotator is purely XML parsing (`mbxml.py` for
  the five MB calls: three data-detail + two search/disc-ID) plus the two monkey-patches
  (`_patched_safe_read`, `_patched_parse_recording`).  The transport, retry, and polite-delay are
  entirely owned by `_net`.
- Sequence before any R3 adapter work so the "uniformly on `_net`" invariant holds from day one.

### AcoustID-seeded wholly-new-release-candidate resolution (deferred from F6)

When `discover()` with `--acoustid-key` finds recording MBIDs from the fingerprint lookup that do not match any existing
candidate (organic search returned nothing), resolve those recording MBIDs to releases via MB and seed wholly-new candidates.
Currently `_enrich_candidates_with_acoustid_seed` only boosts existing candidates — it re-scores candidates whose medium
contains the AcoustID-returned recording MBIDs, but does not create new candidates from scratch.  The boost-existing form is
the F6 deliverable; this richer extension is deferred.  Substrate: `_fetch_acoustid_lookup_raw` (collapsed from
`fetch_acoustid_lookup` in R1/S4; callers slice the `(mbids, cluster_uuid)` tuple) and the existing
`fetch_release` / `fetch_recording_detail` MB wrappers are all in place.  Deferred from F6 (C-F6c Discovery).

### `accuraterip` 4th archival dimension (deferred from PLAN-fingerprint.md)

The archival identity triple (`audio_hash`, `chromaprint_fp`, `acoustid_id`) has a reserved field slot for a 4th dimension:
AccurateRip rip-fidelity (bit-accuracy against crowd consensus of the same pressing).  The `TrackTags` and `TransactionEntry`
models carry a demarcating comment `# --- archival identity (extensible: 4th dim slots in here) ---` so the 4th field appends
without renaming or restructuring.  `audit --enrich` will backfill it via P-FP3 (idempotent maintenance) once the value is
available.  Depends on the **whipper ingest mode** (Act I source adapters) that produces/exposes the AccurateRip result —
music-annotator would read it as rung-0 provenance.  AccurateRip is orthogonal to the three identity values: the triple
answers "what is this / is the audio stable"; AccurateRip answers "was this rip done correctly" (bit-fidelity against a crowd
consensus of the same pressing).

### Miscellaneous

- Audit CE-derived tags: every field populated or explicitly `""`.
- Add cover art type: sleeve front/back.
- When an MBID does not have DiscIDs and comprises multiple media, music-annotator usually selects
  the wrong medium.  Can this be improved?  (Also relevant to Act I ingest quality for the remaining
  dirs.)

---

## Conventions and code stewardship (parallel)

### Public conventions spec — CE anchor + documented extensions  → ROADMAP R6e (finalisation; draft + CE-author contact stay here)

**Posture decided 2026-07-18** after assessing Classical Extras' state: CE the *plugin* is dormant
(author's last commit June 2020, v2.0.11; last maintenance touch Jan 2022 by Picard's lead dev; not
ported to Picard's API v3 — the v3 ecosystem's closest offering is the minimal "Work & Movement"
plugin).  CE the *convention* remains alive and remains our editorial anchor.  The posture:

- **CE stays the anchor for shared tag semantics**; live Picard conventions take precedence where
  they exist (e.g. the `ACOUSTID_ID` Picard-alignment item in III-a — Picard, not CE, is the live
  authority now).
- **Extensions always use new tag names** (`CWP_MOVT_NUM`, `CWP_INTER_INDEX_{i}`, `AUDIO_HASH`, the
  coming rung tag) — additive, never redefining a CE/Picard tag.  Fragmentation is *same name,
  different semantics*; additive extensions don't fragment.
- **No CE plugin code PRs planned** — dormant target, superseded platform (API v2), and a plugin
  model that structurally cannot host music-annotator's distinguishing features (journal,
  verification provenance, batch maintenance, path policy).

**Deliverable**: a public spec document covering the CWP_/CEA_ subset music-annotator implements,
every extension tag with its semantics, every divergence from CE/Picard with rationale (the NOTES
"divergences need a documented rationale" rule, made external), the path-rendering rules
(leaf/intermediate numbering invariants, uniform-ceiling/ragged-floor depth policy), the archival
identity tags, and the sidecar formats.  The spec *is* the contribution: consumable by any future
CE-v3 port, Picard scripter, or other archival tool, at a fraction of the cost of a plugin PR
campaign.  Natural timing: draft anytime; finalise alongside the Act II heuristic freeze so the spec
describes final conventions rather than churning with them.

[jfindlay edit: Let's try contacting the author of the CE plugin and see what their response is to
a) Our extensions/revisions b) Picard APIv3.  We might have to take on/over CEv3.]

### Codebase maintenance cadence — next structural audit

Question raised 2026-07-18: is music-annotator due for another refactor?  **Assessment at raise
time: not now.**  The three-axis structural audit (`PLAN-audit.md`, retired) completed 2026-06 —
C-PROV/C-MOVE primitive extraction, `_pipeline_maint`/`_audit` module splits, `run()` decomposition
into named passes, `__init__` surface shrink, read/mutate CLI verb split — and subsequent commits
have been small increments (caches, colon fix, ENOTDIR fix).  No symptom pressure.

**Trigger-based, not calendar-based.**  The next audit is due **after Act I's structural additions
land** (source adapters, provisional-rung substrate, Discogs integration, `_net`) — the largest
injection of new module boundaries since the last audit; review their coherence once settled, not
while in flight.  Earlier triggers: module bloat, re-triplication of a move/verify/journal-shaped
loop, coverage-gate strain, or an adapter that doesn't fit the pipeline's existing shape.  The
`_net` refactor (Infrastructure) is itself a structural change and carries its own review; it does
not need to wait for this audit.

---

## Execution learnings and graduation tombstones

### Execution learnings (from PLAN-naming.md run)

Durable findings from the naming plan's `/run-plan` chain; recorded here so they survive the plan
deletion.

- **`repath()` intra-plan collision gap (W3a)**: `_assess_collisions` only checks whether the
  destination already exists on disk — it cannot detect that two entries in the same plan recompute
  to the same destination.  Before the W3a fix, `os.replace` would silently overwrite the first
  collision-suffixed file with the second.  Fixed by an intra-plan collision guard (group plan
  entries by recomputed destination before the move loop; skip entries that share a destination).
  Any future repath-style loop must include this guard.

- **`cwp_composer_lastnames` / `cea_composer_lastnames` priority in `build_dest_path` (W2c)**:
  `build_dest_path` prefers `CWP_COMPOSER_LASTNAMES` (from `cwp_composer_lastnames`) over
  `CEA_COMPOSER_LASTNAMES`.  Retroactive tag-patching code that only patches the `cea_` field
  silently produces the wrong path.  Always patch both fields when overriding the composer
  component in `unify()` or any similar retroactive pass.

### Codebase-audit items + destructive-command consistency → GRADUATED to `PLAN-audit.md` (completed, retired)

The four codebase-audit items (handoff from PLAN-naming.md) and the destructive-maintenance-command
confirmation-consistency question (handoff from PLAN-multimedium.md S8/S9) graduated to
`docs/PLAN-audit.md` (2026-06-03), where the user expanded them into a three-axis
structural-coherence audit (app-code / test-code / CLI taxonomy).  The plan ran to completion and was
retired (commit `00a72d6`); the five point-items were resolved as symptoms of larger structural facts
(see the audit commits `cc6ae67`…`45ad512`: C-PROV/C-MOVE primitive, `_pipeline_maint`/`_audit`
splits, `__init__` surface shrink, `run()` decomposition, audit/mutate verb split).
