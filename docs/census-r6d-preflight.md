# Preflight evidence report — consolidated dry-run harness

**Run date:** 2026-08-14  
**Library root:** `~/Remote/hades/Music/Done`  
**Scan status:** SCAN RAN (library mounted, non-empty)

This report captures the three evidence categories required before any destructive library-wide
re-derivation pass runs.  All passes were executed with `dry_run=True` — no file was moved, no tag
was written, no journal entry was appended.

---

## 1. Dry-run change-set (per-pass planned changes)

| Pass | Planned changes | Overlapping files |
|------|---------------:|------------------:|
| repath | 0 | 0 |
| regroup | 0 | 0 |
| unify | **9,009** | 0 |
| enrich | 0 | 0 |
| repatch_catalogue_colon | 0 | 0 |
| repatch_acoustid_tags | 0 | 0 |
| **TOTAL** | **9,009** | **0** |

**Interpretation:**

- `repath` (0): all files are already at their correct depth-clamped paths.
- `regroup` (0): no confirmed split-release fragments to consolidate.
- `unify` (9,009): the dominant change — composer-split top-level directories need to be unified
  under the canonical `"Composer; Ensemble - Release"` form.  This is the primary work of the
  destructive pass.
- `enrich` (0): all files already have complete enrichment tags.
- `repatch_catalogue_colon` (0): no catalogue-colon corruption remains (already clean).
- `repatch_acoustid_tags` (0): no legacy `CHROMAPRINT_FP` keys remain (migrated by the AcoustID
  repatch pass).

**Cross-pass overlap map:** none.  No file appears in more than one pass's plan.  Since `unify` is
the only active pass, pass-ordering constraints (tag-content before repath) do not apply to this
run.

---

## 2. Journal capacity

| Metric | Value |
|--------|------:|
| Current entry count | 47,559 |
| Current on-disk size | 48,780,316 bytes (~46.5 MiB) |
| Projected delta (unify) | +9,009 entries |
| Projected total after run | 56,568 entries |

**Note:** The journal is rewritten in full on every append.  A run of 9,009 `unify` moves will
append 9,009 entries, each triggering a full rewrite.  The projected post-run journal size is
approximately 55 MiB (linear extrapolation from current 46.5 MiB / 47,559 entries).  This is
within normal filesystem capacity; no streaming-append rewrite is required before the destructive
pass.

---

## 3. Reference/ retention evidence

| Metric | Value |
|--------|-------|
| `Reference/` directory | **present** at `~/Remote/hades/Music/Reference` |
| Disk footprint | 428,656,585,991 bytes (~399 GiB) |

**Decision required (human):** Whether to retain `Reference/` through the destructive pass is a
human go/no-go decision.  The evidence above supports that decision:

- `Reference/` is present and covers ~399 GiB — a full pre-annotation library snapshot.
- The destructive pass (`unify`, 9,009 moves) will restructure top-level directories.
- Retaining `Reference/` provides a non-destructiveness safety net; deleting it before the pass
  reclaims ~399 GiB of disk space.

This report does not automate the retention decision.

---

## Summary for go/no-go

| Evidence category | Finding |
|-------------------|---------|
| Dry-run change-set | 9,009 `unify` moves; 0 repath/regroup/enrich/repatch |
| Cross-pass overlaps | None — single-pass ordering constraints do not apply |
| Journal capacity | 47,559 → 56,568 entries; ~46.5 → ~55 MiB; within capacity |
| Reference/ retention | Present at ~399 GiB; retention decision deferred to operator |

The destructive pass is a `unify`-only operation at this library state.  The other deferred passes
(`repath`, `repatch_catalogue_colon`, `repatch_acoustid_tags`) have already been applied or are
not needed.  The go/no-go decision rests on: (a) operator confirmation that `unify`'s 9,009
planned moves are correct, and (b) the `Reference/` retention decision above.

---

*To re-run this scan after library changes:*
```
scripts/preflight_r6d.py --root ~/Remote/hades/Music/Done --json docs/census-r6d-preflight.json
```
