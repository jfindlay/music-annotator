<!-- juncture-tier: opus -->
<!-- sub-track: R4b (cross-medium fragmentation inventory) — library-completion arc (docs/ROADMAP.md), issue A-c
     (docs/BACKLOG.md).  Inventory-first / remedy-deferred: enumerate what still fragments, then route remedies to
     B-track (MB data) or III-b (regroup) — no remedy code in this sub-track.  juncture-tier: opus retained from the arc
     default not for the inventory (read-only, low correctness-criticality) but for the S2 ◆ remedy-routing judgment,
     which feeds J2/R6 planning where a mis-route propagates. -->

# PLAN — R4b: cross-medium fragmentation inventory (enumerate → route)

## Purpose (design intent)

*(Re-read at every session start — anti-defocus anchor.)*

Cross-medium MB attributions/annotations still fragment the filetree in some cases **despite** the C-S0 all-media
aggregation substrate and the `audit`/`regroup` detect→adjudicate→act cycle (`_audit.py`, `docs/NOTES.md` "The `regrouped`
journal obligation").  R4b **inventories what still fragments before designing any remedy** (BACKLOG A-c: "enumerate before
designing anything; the remedy may be mostly B-track (MB data corrections) or III-b (regroup passes) once enumerated").

The deliverable is a **census artifact** (`docs/census-fragmentation.{md,json}`) produced by a **read-only scanner** in
`scripts/`, following the R0 (`census-r0.{json,md}`) and V1a-S3 / `scan_nonuniform_depth.py` precedents.  **No `src/`
changes, no remedy code, no path or tag mutation** — R4b is pure inventory.  The residual fragmentation shapes it targets
are the ones the existing `_audit.py` detector *cannot see* because that detector keys on `release_id`:

- **box sets MB models as multiple releases** — one release-group, many release MBIDs, spread across top_dirs;
- **per-medium artist-credit differences** — one release whose media carry different artist credits, splitting the path;
- **release-vs-release-group attribution splits** — attribution keyed on release vs release-group diverging.

R4b runs **parallel to the styleguide arc** (`docs/ROADMAP-styleguide.md`); its findings about attribution-driven
fragmentation feed that arc's case register (normalisation cases especially — ROADMAP-styleguide cross-arc coupling).  It is
**largely independent of attribution policy** (inventory-first), so it does not wait on styleguide v1.

## Verify gate

The scanner lands in `scripts/`, which is **outside** the `src/`+`tests/` gate scope (`mypy` and `pytest` run `src/ tests/`
only — see `pyproject.toml`).  The scanner's type/lint discipline is documented but **not gate-enforced**, exactly like
`census_original.py` and `scan_nonuniform_depth.py`.  The stated gate (discovered, not assumed — no `make` in this repo):

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (pytest + 100% branch coverage).  Not exercised by R4b unless a scanner helper
  is promoted into `src/` (it should not be — keep the scanner standalone per precedent).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict).  Same: does not cover `scripts/`.
- The real per-session gate is: **S1** — scanner runs clean under `venv/bin/python -m py_compile` and (best-effort)
  `venv/bin/mypy scripts/<file>`, produces well-formed JSON on the reference or documentary corpus; **S2** — the census
  artifact is complete (every detected shape classified and remedy-routed) plus the ◆ boundary review.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 | R4b Add read-only cross-medium fragmentation scanner + shape taxonomy | A | Sonnet | C-W2, C-CLASS, `_audit.py` grouping | `scripts/scan_fragmentation.py` |
| 2 ◆ | R4b Census library fragmentation shapes and route remedies | I | @architect (Opus) | **C-FRAG-TAX**, the scanner, censuses (r0/library) | `docs/census-fragmentation.md`, `docs/census-fragmentation.json` |

`Cat`: **S1 is A (substrate)** — it freezes **C-FRAG-TAX**, the fragmentation-shape vocabulary that S2's classification and
any downstream remedy sub-track consume; over-specify it (Category-A discipline — carry a shape column now even if the scan
finds zero instances).  **S2 is I (integrative)** — the enumerate→classify→route synthesis; integrative sessions are
consistently under-scheduled, so full session minimum, do not compress.

`Tier`: **S1 Sonnet** — mechanical scanner authored against a frozen precedent (`scan_nonuniform_depth.py`) and a stable
read-only substrate (`_audit.py`); cost-of-wrong is low (read-only, caught when S2 consumes the output).  **S2 @architect
(Opus)** — the remedy-routing judgment (each shape → B-track vs III-b vs no-op) is where a wrong call propagates into
J2/R6 planning; this is the ◆ boundary and the reason the sub-track's `juncture-tier` stays `opus`.

**Sizing (levers named).**  Two sessions, split at a **contract-sharp boundary**: S1 freezes C-FRAG-TAX (the shape
vocabulary), S2 consumes it — the legitimate split point (one half freezes an interface the other consumes).  **Lever 2
(the floor):** the scanner + its shape taxonomy is one irreducible unit (you cannot enumerate box-set splits without also
settling per-medium-credit and RG-vs-release splits — they share the release-group join key and overlap), so S1 is not
fractured below it.  **Lever 4 (correctness-criticality) is low** (read-only, no mutation) and **lever 5 (inner loop) is
strong** (100% branch coverage, strict mypy, pylint 10/10) — both pull toward small units, confirming two rather than one.
**One-line-commit-title check:** both rows pass ("Add read-only … scanner …", "Census … and route remedies").  Merging
into one would put a code deliverable and a large adjudicative-prose deliverable under a single commit — two titles.
**Known risk (additive-reshard signal):** if the scan surfaces a large, structurally-varied population (as R0's 6-shape
depth taxonomy needed its own session), S2 may split into scan-classify and route-handoff — decided live at the S1 ◆ from
the population size, not pre-provisioned (D-3 below).

## Session detail

### S1 — Add read-only cross-medium fragmentation scanner + shape taxonomy — freezes C-FRAG-TAX

**Deliverable.**  `scripts/scan_fragmentation.py`: a standalone read-only scanner (machine-specific `ROOT`, "not part of
the package", precedent `scan_nonuniform_depth.py`) that walks the annotated library and emits per-shape findings as
well-formed JSON (for the S2 `.json` artifact) plus a human summary.  Three residual-shape passes, each grouping embedded
tags by a **different join key** than the existing `release_id`-keyed detector:

1. **RG-multi-release (box set):** group audio files by `MUSICBRAINZ_RELEASEGROUPID` (already embedded — `_tags.py:916`,
   `musicbrainz_releasegroupid`); flag release-groups whose files carry ≥2 distinct `MUSICBRAINZ_ALBUMID` **and** span ≥2
   top_dirs (the shape `_audit.detect_fragmented_releases` misses because it keys on album, not RG).
2. **Per-medium artist-credit variance:** within one `MUSICBRAINZ_ALBUMID`, compare the rendered artist-credit / albumartist
   across media (disc subdirs); flag releases whose media disagree on the credit that drives the top_dir/work_dir path.
3. **RG-vs-release attribution split:** flag where attribution keyed on release vs release-group would place the same
   conceptual work in different paths.

Reuse `_audit.py`'s grouping patterns (`_journal_fragmentation_groups`, `detect_fragmented_releases`) as the structural model
— **read the tag, group, threshold** — but the scanner is standalone in `scripts/` (does not import into `src/`; does not
touch the gate).  Freezes **C-FRAG-TAX**: the enumerated shape vocabulary + each shape's JSON record schema.

**Coverage assertion (the session's KAT-analog).**  A KAT: run the scanner against a **synthetic fixture library** (a
handful of tagged FLAC/MP3 stubs constructed to exhibit exactly one instance of each of the three shapes plus one clean
release) and assert the JSON output contains exactly those shape records and no false positives on the clean release.  This
is the deliverable's contract — if it can't be a KAT, C-FRAG-TAX is under-defined (flag it).  The fixture uses the minimal
FLAC/MP3 byte constants already embedded in `test_pipeline.py` / `test_integration.py` as the tagging substrate.

**Subtleties.**
- **Over-specify the taxonomy (Category-A).**  Carry a JSON `shape` field with all three shapes as an enumerated set even
  if the live/reference scan finds zero of one — adding a shape to the schema later (and re-running the scan) is costlier
  than carrying an empty bucket.  Include a `remedy_route` field (`b-track` / `iii-b` / `no-op` / `undetermined`) written
  empty by S1 and filled by S2 — freeze the *field*, defer the *value*.
- **Host-path caveat (D-1).**  `ROOT` is machine-specific; a mismatched root is a **silent no-op hazard** (same trap the
  V1a-S3 census hit — ROADMAP-styleguide line 89; census-library was produced from documentary evidence because hades was
  not mounted, D-A2).  The scanner must print `files read: N` up front and refuse to emit an "empty = clean" census; an
  unreadable/empty root is reported as *scan-not-run*, never as *no fragmentation*.
- Reuse `_read_albumid_tag` / the `MUSICBRAINZ_RELEASEGROUPID` read pattern; do not re-derive tag-reading.

**Deferrals.**  No classification, no remedy routing, no census prose (S2).  No `src/` changes.  No remedy code ever
(B-track / III-b, post-inventory).

### S2 ◆ — Census library fragmentation shapes and route remedies

**Deliverable.**  Run `scripts/scan_fragmentation.py` against the reference corpus (live hades if mounted; else a
documentary corpus reconstructed from `census-r0`, the journal, and NOTES — the D-A2 posture, operator-cleared for
census-library).  Author `docs/census-fragmentation.{md,json}`: every detected instance classified by C-FRAG-TAX shape and
assigned a **remedy route** — `b-track` (MB data correction), `iii-b` (regroup pass), or `no-op` (not real fragmentation /
faithful).  The `.md` carries the per-shape frequency distribution and the routing rationale; the `.json` is the
machine-readable census (scanner output enriched with `remedy_route` values).  End with the ◆ boundary handoff: which shapes
route where, what each route's follow-on sub-track is, and any conflict with a frozen contract (C-S0 / C-CLASS / C-INIT →
flag to the arc boundary, never re-open in-arc).

**Coverage assertion.**  Zero detected instances without a shape classification; zero classified instances without a
remedy route; the routing rationale cites the layer-routing rule (renderer/policy = A, MB data = B, scholarship = C —
`docs/ROADMAP.md` design intent) for each route.  A shape with zero instances is recorded as *enumerated, zero live
instances* (a valid census result, not a gap).

**Subtleties.**
- Integrative sessions are consistently under-scheduled — full session minimum.
- The remedy is **routed, not built** (BACKLOG A-c).  R4b never writes B-track corrections or III-b regroup code; it names
  the route and the follow-on.  A shape that looks like it needs *new* detection machinery in `src/` (not a route to an
  existing remedy) is an additive-reshard signal, not an in-session build.
- **Feed the styleguide arc.**  Attribution-driven fragmentation findings (per-medium credit variance especially) are
  NORM-case evidence for the styleguide register (ROADMAP-styleguide cross-arc coupling) — surface them as a capture for
  that arc, do not adjudicate them here.
- An apparent conflict with C-S0 (all-media aggregation) or C-CLASS/C-INIT (path grammar) is a **finding for the
  library-completion arc's boundary**, never an in-arc contract change.

**Deferrals.**  All remedy implementation (B-track MB edits; III-b regroup passes — post-inventory, own shards).  Any
styleguide-register adjudication of the surfaced NORM cases (that arc owns it).

## Cross-session contracts

### C-FRAG-TAX — fragmentation-shape taxonomy + JSON record schema *(to be frozen at S1)*

The shape vocabulary the census classifies against and any downstream remedy sub-track consumes.  Enumerated shapes:
`rg-multi-release` (box set), `per-medium-credit-variance`, `rg-vs-release-split`.  JSON record schema per finding:
`shape`, the join-key identifiers (release_group_id / album_id(s) / top_dirs), the backing file paths, and a
`remedy_route` field (`b-track` / `iii-b` / `no-op` / `undetermined`) written empty by S1, filled by S2.  **Flavour:
prose- + test-enforced** (prose vocabulary; the S1 KAT enforces the record schema against the synthetic fixture).
**Defined-in:** S1 (`scripts/scan_fragmentation.py` + its docstring taxonomy).  **Consumed-by:** S2 (classification +
routing), any post-R4b remedy sub-track.  Over-specified by design (Category-A: all three shapes carried even at zero
live instances; `remedy_route` field frozen ahead of its values).

### Consumed (frozen upstream — invalidation is out-of-scope for R4b)

- **C-W2** (fragmentation join key = embedded tag, not journal — `_audit.detect_fragmented_releases`): R4b groups by
  embedded `MUSICBRAINZ_RELEASEGROUPID` / `MUSICBRAINZ_ALBUMID`, extending the same tag-is-authority posture to the
  release-group axis.  **Flavour: prose-enforced (C-W2 in `_audit.py`).**
- **C-CLASS / C-INIT** (R4a, library-completion arc): the top-level class + initial-component path grammar the scanner must
  parse to attribute a file to its top_dir/work_dir (`_work_top_dir` handles both legacy two-level and class-prefixed
  three-level paths).  Validate-only; a conflict is a finding for the arc boundary.  **Flavour: compiler-enforced upstream.**
- **C-S0** (all-media aggregation spans media; mutation does not): R4b inventories what fragments *despite* C-S0 — a finding
  that C-S0 itself under-aggregates is an arc-boundary finding, not an in-arc re-freeze.  **Flavour: prose-enforced.**
- The **read-only scanner precedent** (`scan_nonuniform_depth.py`, `census_original.py`): standalone, machine-specific
  `ROOT`, not imported into `src/`, produces a `docs/census-*.{md,json}` artifact.

### Produced

- **C-FRAG-TAX** at S1 (above).  The **census-fragmentation artifact** at S2 — the input to the (routed, deferred) B-track
  and III-b remedy shards and NORM-case evidence for the styleguide arc.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | R4b Add read-only cross-medium fragmentation scanner + shape taxonomy | pending | — | C-FRAG-TAX |
| 2 | R4b Census library fragmentation shapes and route remedies | pending | — | census-fragmentation |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **D-1 (host-path silent-no-op hazard — carried from V1a-S3 / D-A2).**  The scanner's `ROOT` is machine-specific; a
  mismatched or unmounted library root reads zero files.  The scanner MUST distinguish *scan-not-run* (empty/unreadable
  root) from *no-fragmentation* (root scanned, zero findings) — the former is never reported as clean.  If hades is not
  mounted in the dev environment (as at census-library time), S2 produces a **documentary** census on the D-A2 posture
  (operator-cleared basis), not a live scan.  **Internal-continue** (S1 handles it structurally).
- **D-2 (remedy is routed, not built).**  BACKLOG A-c: R4b enumerates; remedies are mostly B-track (MB data) or III-b
  (regroup) and are **separate post-inventory shards**.  A finding that appears to need new `src/` detection machinery is
  an **additive-reshard** signal, not an in-session build.
- **D-3 (S2 volume — additive-reshard signal).**  If the scan surfaces a large, structurally-varied population (cf. R0's
  6-shape depth taxonomy that needed its own session), S2 may split into scan-classify + route-handoff at the natural
  boundary.  **Additive-reshard**, decided live at the S1 ◆ from the observed population size — not pre-provisioned.
- **D-4 (contract-conflict = arc-boundary finding, not in-arc HALT).**  An apparent conflict with C-S0 / C-CLASS / C-INIT
  is a finding forwarded to the library-completion arc's boundary (ROADMAP Discoveries), never an in-arc contract change.
  Only a discovery that R4b's own read-only posture is unsafe would be **destructive-HALT** — not expected for an
  inventory sub-track.
- **D-5 (cross-arc feed to the styleguide register).**  Per-medium credit-variance findings are NORM-case evidence for
  `docs/ROADMAP-styleguide.md`; surface them as a capture for that arc, do not adjudicate in R4b.  **Internal-continue.**

## Notes for executors

- **Tier routing.**  S1 Sonnet (mechanical scanner on a frozen precedent, read-only, low cost-of-wrong).  S2 @architect on
  Opus (the ◆ remedy-routing judgment feeds J2/R6 — the reason `juncture-tier: opus` is retained despite the read-only
  inventory being low-criticality).
- **Register: inventory, not remedy.**  R4b enumerates and routes; it never writes a B-track MB correction or III-b regroup
  pass.  The census artifact is the deliverable; remedies are separate downstream shards.
- **Invariants to preserve:** read-only throughout (no file moves, no tag/path mutation, no journal writes); tag-is-authority
  join key (C-W2, extended to release-group); the scanner stays standalone in `scripts/` (never imported into `src/`, never
  in the gate scope); C-S0 / C-CLASS / C-INIT validate-only (conflicts are arc-boundary findings); the scan-not-run vs
  no-fragmentation distinction (D-1) is never collapsed.
- **Sequencing.**  S1 → S2 strictly serial (C-FRAG-TAX gates the classification).  On the S2 ◆: the census routes each shape
  to B-track / III-b / no-op; the routed remedy shards become shardable (own PLANs, coordinate III-b passes with R6d's
  one-pass re-derivation per the arc's "make the library more like itself once" intent).  R4b closes; R4a + R4b done means
  the R4 tail's structural half is complete — J2 still waits on the styleguide arc's v1 for the editorial half.
- **Suggested first `/plan-run` invocation (unproven shard pattern — halt at boundaries):**
  `/plan-run docs/PLAN-fragmentation.md --halt-at-boundaries` (the R4b shard pattern is new for this arc; halt at the S2 ◆
  for the remedy-routing review before the census is treated as authoritative).  Note the non-default PLAN path — this
  sub-track's rolling detail is `docs/PLAN-fragmentation.md`, not `docs/PLAN.md` (which holds the live styleguide V1b
  sub-track; the repo convention is named per-sub-track PLAN files — ROADMAP.md line 5).
