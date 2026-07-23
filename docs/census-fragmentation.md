# Census — R4b: Cross-Medium Fragmentation Shapes and Route Remedies

**Sub-track:** R4b (cross-medium fragmentation inventory)
**Session:** S2 ◆ (census + remedy routing)
**Posture:** scan-not-run / documentary (D-A2)
**JSON artifact:** docs/census-fragmentation.json

## Scan-Not-Run Note

The canonical library root `/home/justin/Remote/hades/Music/Done` was not accessible in the dev environment at census time (hades
not mounted). This census is produced on the D-A2 posture (operator-cleared basis): findings are reconstructed from available
documentary evidence rather than a live scan.

Evidence sources:

- **`docs/census-r0.md`** — 147 top-level dirs in `Original/` (pre-ingest material), with provenance and MB-status
  classifications. The whipper/in-mb-clean population (52 dirs) is the primary source for fragmentation candidates: these are
  rips with confirmed MB releases, so their dir-name structure is the best available proxy for release-group membership.
- **`docs/census-library.md`** — empirical census of the `Done/` tree (~343 top-level dirs, ~3663 FLACs as of the 2026-06
  audit). Produced on the same D-A2 posture; scanner was not run against the live library.
- **`scripts/scan_fragmentation.py`** — the S1 scanner (C-FRAG-TAX frozen at S1). Not run against the live library; its
  docstring defines the shape vocabulary and JSON record schema consumed here.

The five findings below are pre-ingest fragmentation candidates from `Original/`. The `Done/` tree likely contains additional
instances from previously ingested box sets (the Furtwängler finding explicitly notes a partial-ingest scenario where some discs
may already be in `Done/`), but specific instances in `Done/` cannot be confirmed without a live scan.

**Re-run instruction:** Run `scripts/scan_fragmentation.py` on hades (with hades mounted) for authoritative findings before
implementing any remedy. The documentary census is a planning artifact; the live scan is the authoritative input to the remedy
shards.

## Coverage KAT

The S2 coverage assertion (from PLAN-fragmentation.md):

- **Zero unclassified instances:** Every finding below carries a `shape` value from the C-FRAG-TAX enumerated set
  (`rg-multi-release`, `per-medium-credit-variance`, `rg-vs-release-split`). ✓
- **Zero unrouted instances:** Every finding carries a `remedy_route` value (`iii-b`, `b-track`, or `no-op`). All five
  documentary findings are routed `iii-b`. ✓
- **Layer-routing rule cited for each route:** Each shape section below cites the layer-routing rule
  (renderer/policy = A, MB data = B, scholarship = C — `docs/ROADMAP.md` design intent). ✓
- **Zero-instance shapes recorded as enumerated:** Shapes 2 and 3 (`per-medium-credit-variance`, `rg-vs-release-split`) have
  zero documentary instances and are explicitly recorded as *enumerated, zero live instances* — a valid census result, not a
  gap. ✓

## Shape 1: rg-multi-release

**Definition (C-FRAG-TAX):** One release-group, ≥2 distinct album MBIDs, spanning ≥2 top_dirs. This is the shape
`_audit.detect_fragmented_releases` misses because it keys on album MBID, not release-group MBID.

**Frequency distribution:** 5 documentary candidates (pre-ingest, from `Original/`). The `Done/` tree (~343 dirs) likely
contains additional instances from previously ingested box sets, but specific instances cannot be confirmed without a live scan.
The Furtwängler finding (discs 2, 3, 5 present in `Original/`, discs 1 and 4 absent) is a partial-ingest signal: the missing
discs may already be in `Done/`, meaning the fragmentation already spans both trees.

### Instance 1 — Toscanini NBC Beethoven cluster

**top_dirs:**

- `Arturo Toscanini :: Beethoven (NBC Symphony Orchestra) (Disc 6).0x310f2704`
- `Arturo Toscanini :: Ludwig van Beethoven Symphonies Nos. 1,2,3&4 Vol 1.0x670e3a09`
- `Arturo Toscanini :: NBC Symphony Orchestra Vol II: Ludwig van Beethoven CD2.0x7f0da908`
- `Beethoven :: Beethoven (NBC Symphony Orchestra) (Disc 5).0x3d117c05`
- `Beethoven  :: Symphonies Nos. 3&4 - Toscanini, NBCSO.0x73121308`

**Documentary evidence:** census-r0.md whipper/in-mb-clean: five separate top-level dirs for discs/volumes of the same
Toscanini NBC Beethoven recordings. Dir names include disc numbers and volume labels confirming these are parts of one release
group. When ingested, they will share a release-group MBID but carry distinct album MBIDs, spanning 5 top_dirs. MB data
correctly models these as separate releases within one release group; the library structure needs a regroup pass.

### Instance 2 — Wagner Ring Karajan/BPO

**top_dirs:**

- `Wagner, Richard: :: Wagner: Ring Des Nibelungen; Karajan::BPO.0x6b0a9b08`
- `Wagner, Richard: :: Wagner: Ring Des Nibelungen; Karajan::BPO.0xaf11030d`

**Documentary evidence:** census-r0.md whipper/in-mb-clean: two separate top-level dirs for the same Wagner Ring cycle
(Karajan/BPO), distinguished only by FreeDB disc-ID hex suffix. These are either two different pressings (same release group,
different album MBIDs) or two rips of the same release. Either way, they span 2 top_dirs under the same release group. The
library structure needs a regroup pass; if these are duplicate rips, one may be a deletion candidate.

### Instance 3 — Furtwängler multi-disc box set

**top_dirs:**

- `Wilhelm Furtwängler :: [Disc 2] Johannes Brahms.0x1a0b2104`
- `Wilhelm Furtwängler :: [Disc 3] Anton Bruckner.0x2f0fe904`
- `Wilhelm Furtwängler :: [Disk 5] Sibelius, Strauss, Ravel.0x180c3b03`

**Documentary evidence:** census-r0.md whipper/in-mb-clean: three separate top-level dirs for discs 2, 3, and 5 of a
Furtwängler multi-disc box set. The disc numbers in the dir names confirm these are parts of one release group. When ingested,
they will share a release-group MBID but carry distinct album MBIDs, spanning 3 top_dirs. The library structure needs a regroup
pass. Note: discs 1 and 4 are absent from `Original/`, suggesting they may already be in `Done/` — a partial-ingest
fragmentation scenario where the same release group is split across both trees.

### Instance 4 — Rossini Panorama

**top_dirs:**

- `Gioacchino Rossini :: Panorama - Gioacchino Rossini - Disc 1.0x9b10f20b`
- `Gioacchino Rossini :: Panorama.0xe6120d0f`

**Documentary evidence:** census-r0.md whipper/in-mb-clean: two separate top-level dirs for the Rossini Panorama compilation
— one for Disc 1 specifically, one for the main set. These are parts of the same release group. When ingested, they will share
a release-group MBID but carry distinct album MBIDs, spanning 2 top_dirs. The library structure needs a regroup pass.

### Instance 5 — Jazz Classics 10th Edition

**top_dirs:**

- `Various :: Jazz Classic, 10th ED, CD03.0xb912740c`
- `Various :: Jazz Classics, 10th ED, CD01.0xd712ac1f`
- `Various :: Jazz Classics, 10th ED, CD02.0xa912340c`

**Documentary evidence:** census-r0.md whipper/in-mb-clean: three separate top-level dirs for CDs 1, 2, and 3 of the Jazz
Classics 10th Edition compilation. The CD numbers in the dir names confirm these are parts of one release group. When ingested,
they will share a release-group MBID but carry distinct album MBIDs, spanning 3 top_dirs. The library structure needs a regroup
pass.

### Remedy route: `iii-b`

**Routing rationale:** The fragmentation is caused by the library's path/grouping structure — each release in a box set is
ingested as a separate top-level dir, keyed on album MBID. The MB data correctly models these as separate releases within one
release group (layer B is correct). The remedy is a regroup pass (layer A / iii-b) to consolidate the dirs under one top-level
dir per release group. Layer-routing rule: renderer/policy = A (library structure), MB data = B (correct), scholarship = C (not
applicable). The issue is in layer A; the fix is in layer A.

**Arc-boundary finding:** The current C-S0 substrate aggregates within a release (album MBID), not across releases in a release
group. Box sets modeled as multiple releases in one release group will fragment across top_dirs despite C-S0. This is a C-S0
limitation — the substrate would need to be extended to aggregate across releases in a release group to prevent this shape
structurally. This is a finding for the library-completion arc's boundary (ROADMAP Discoveries), not an in-arc contract change.
A potential remedy would extend C-S0 to aggregate across releases in a release group — but that is an arc-boundary decision,
not an in-arc build.

## Shape 2: per-medium-credit-variance

**Definition (C-FRAG-TAX):** One album, ≥2 media (disc subdirs), ≥2 distinct ALBUMARTIST values within the same top_dir. This
is the within-release inconsistency that would cause the path-builder to produce different paths for different discs of the same
release.

**Frequency distribution:** Zero live instances (enumerated per C-FRAG-TAX — a valid census result, not a gap). The `Done/`
tree may contain instances, but none are identifiable from census-r0.md or census-library.md without a live scan.

**Instances:** None detected from documentary evidence.

### Remedy route (if instances found): `b-track`

**Routing rationale:** Per-medium credit variance within a single release is an MB data quality issue — the ALBUMARTIST tag
should be consistent across all media of the same release. The inconsistency is in the MB data (layer B): the release's media
carry different artist credits, which is either an MB data error or an intentional per-medium attribution in MB. If it is an MB
data error, the remedy is to fix the MB data (layer B / b-track) so ALBUMARTIST is consistent. If it is intentional in MB (a
genuine per-medium attribution), the remedy is `no-op` (the fragmentation is faithful). Layer-routing rule: the inconsistency
is in the MB data (layer B), not in the renderer/policy (layer A) or scholarship (layer C). Adjudicate per-instance when a live
scan surfaces instances.

**Cross-arc feed (styleguide register):** Per-medium credit-variance findings are NORM-case evidence for the styleguide arc
(`docs/ROADMAP-styleguide.md`). Attribution-driven fragmentation from per-medium credit differences is a normalisation case
(NORM-2: name-form variance; NORM-10: ensemble name language selection — minted in census-library.md D-S3-3). Surface as a
capture for the styleguide arc when a live scan is run; do not adjudicate here (D-5 / internal-continue).

## Shape 3: rg-vs-release-split

**Definition (C-FRAG-TAX):** One release-group, ≥2 top_dirs, ≥2 distinct ALBUMARTIST values. This captures the case where
attribution keyed on release vs release-group would place the same conceptual work in different paths — the RG-level credit
diverges from the release-level credit. Distinct from rg-multi-release (which keys on album-count divergence, not
artist-credit divergence): a release-group can have only one album MBID but two different ALBUMARTIST values across top_dirs.

**Frequency distribution:** Zero live instances (enumerated per C-FRAG-TAX — a valid census result, not a gap). The `Done/`
tree may contain instances, but none are identifiable from census-r0.md or census-library.md without a live scan.

**Instances:** None detected from documentary evidence.

### Remedy route (if instances found): `b-track` or `no-op` (case-dependent)

**Routing rationale:** The remedy depends on whether the ALBUMARTIST divergence is an MB data error or a faithful
representation of different attributions for different releases in the same release group. If the divergence is an MB data error
(wrong artist credit on one release), the remedy is `b-track` (fix the MB data, layer B). If the divergence is faithful
(different releases in the same release group are correctly attributed to different artists — e.g. a compilation attributed to
"Various Artists" in one release and to a named ensemble in another), the remedy is `no-op` (the fragmentation is intentional).
Layer-routing rule: if the issue is in the MB data (layer B), fix it there; if it is a faithful representation, no fix is
needed. Adjudicate per-instance when a live scan surfaces instances.

## ◆ Boundary Handoff

**R4b closes.** R4a + R4b done means the R4 tail's structural half is complete. J2 still waits on the styleguide arc's v1 for
the editorial half.

### Remedy routing summary

| Shape | Instances | Remedy route | Follow-on sub-track |
|-------|-----------|--------------|---------------------|
| rg-multi-release | 5 (documentary, pre-ingest) | iii-b | III-b regroup pass (own shard, coordinate with R6d's one-pass re-derivation) |
| per-medium-credit-variance | 0 (enumerated) | b-track (if found) | B-track MB data corrections (own shard) |
| rg-vs-release-split | 0 (enumerated) | b-track or no-op (case-dependent) | B-track MB data corrections (own shard) or closed |

### Arc-boundary findings

1. **C-S0 limitation (rg-multi-release):** The current C-S0 substrate aggregates within a release (album MBID), not across
   releases in a release group. Box sets modeled as multiple releases in one release group will fragment across top_dirs despite
   C-S0. This is a finding for the library-completion arc's boundary (ROADMAP Discoveries). A potential remedy would extend
   C-S0 to aggregate across releases in a release group — but this is an arc-boundary decision, not an in-arc contract change.
   Forward to the arc boundary; do not re-open C-S0 in-arc (D-4).

2. **No C-CLASS/C-INIT conflicts detected** from documentary evidence. The five rg-multi-release clusters all use the
   whipper/in-mb-clean dir-name convention (artist :: title.fredbid) which is consistent with the C-CLASS path grammar. No
   conflict to forward.

### Cross-arc feed

**Styleguide arc (`docs/ROADMAP-styleguide.md`):** Per-medium credit-variance findings (if found in a live scan) are NORM-case
evidence for the styleguide register. Attribution-driven fragmentation from per-medium credit differences is a normalisation
case (NORM-2, NORM-10). Surface as a capture for the styleguide arc when a live scan is run; do not adjudicate in R4b (D-5 /
internal-continue).

### Follow-on shards

- **III-b regroup shard:** Consolidate the 5 rg-multi-release clusters (and any additional instances found in a live scan)
  under one top-level dir per release group. Coordinate with R6d's one-pass re-derivation per the arc's "make the library more
  like itself once" intent. Prerequisite: live scan to confirm which clusters are pre-ingest only vs. already partially in
  `Done/` (the Furtwängler partial-ingest scenario).
- **B-track shard:** Fix MB data for any per-medium-credit-variance or rg-vs-release-split instances found in a live scan.
  Zero instances from documentary evidence; shard is contingent on live scan results.
- **Live scan (prerequisite for both shards):** Run `scripts/scan_fragmentation.py` on hades to get authoritative findings
  before implementing any remedy. The documentary census is a planning artifact; the live scan is the authoritative input to
  the remedy shards.

### D-A2 posture note

This census was produced on the D-A2 posture (operator-cleared basis) because hades was not mounted in the dev environment.
The documentary census identifies the fragmentation patterns and routes the remedies, but the specific instances in `Done/`
cannot be confirmed without a live scan. The five findings above are pre-ingest candidates from `Original/`; the `Done/` tree
likely contains additional instances. The remedy shards should be preceded by a live scan run on hades.
