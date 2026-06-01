#!/usr/bin/env python3
"""Scan the annotated library for works whose tracks have non-uniform hierarchy depth.

A "work-group" is the set of all tracks (across all subdirectories of one release dir)
that share a CWP_WORKID_TOP.  Non-uniform depth = tracks in one group carrying different
CWP_PART_LEVELS values.  We also record, per group, the shape of the split: which depth
the majority sit at, which the minority, and how many distinct bottom-works hold >1 track
(the multi-recording-per-bottom-work condition that also drives the leaf-numbering bug).

Output is grouped by release directory so each finding is locatable on disk.

Ad-hoc analysis tool, not part of the package.  Produces the census summarised under
"Hierarchy-depth normalisation (deferred L2 ...)" in ``docs/BACKLOG.md``; re-run it when
the deferred L2 depth-normalisation work reopens to refresh the shape distribution against
a more complete library.  ``ROOT`` is machine-specific — adjust to the local library root.
"""
from __future__ import annotations

import glob
import os
from collections import Counter, defaultdict

from mutagen.flac import FLAC

ROOT = os.path.expanduser("~/Remote/hades/Music/Done")


def g(audio: FLAC, key: str) -> str:
    v = audio.get(key)
    return v[0] if v else ""


def main() -> None:
    files = sorted(glob.glob(os.path.join(ROOT, "**", "*.flac"), recursive=True))
    # Group every track by (release_dir, workid_top).  release_dir = the dir two levels
    # under ROOT is unreliable (opera acts nest deeper), so use the dir that contains the
    # [rec/rel YYYY] suffix: walk up until the parent is an artist dir directly under ROOT.
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    read_errors = 0
    for f in files:
        try:
            a = FLAC(f)
        except Exception:  # noqa: BLE001
            read_errors += 1
            continue
        rel = os.path.relpath(f, ROOT)
        parts = rel.split(os.sep)
        # parts[0] = artist dir; parts[1] = work/release dir (carries the year suffix).
        release_dir = os.path.join(parts[0], parts[1]) if len(parts) >= 2 else parts[0]
        wid = g(a, "CWP_WORKID_TOP")
        groups[(release_dir, wid)].append(
            {
                "file": rel,
                "part_levels": g(a, "CWP_PART_LEVELS"),
                "workid_top": wid,
                "work_top": g(a, "CWP_WORK_TOP"),
                "workid_0": g(a, "CWP_WORKID_0"),
                "part_0": g(a, "CWP_PART_0"),
                "ordering_key_0": g(a, "CWP_ORDERING_KEY_0"),
                "title": g(a, "TITLE"),
            }
        )

    nonuniform: list[tuple[str, str, list[dict[str, str]]]] = []
    multi_rec_per_bottom: list[tuple[str, str, list[dict[str, str]]]] = []
    for (release_dir, wid), tracks in groups.items():
        depths = {t["part_levels"] for t in tracks}
        if len(depths) > 1:
            nonuniform.append((release_dir, wid, tracks))
        # Independent signal: bottom works that hold >1 recording (leaf-collision driver).
        bottom_counts = Counter(t["workid_0"] for t in tracks if t["workid_0"])
        if any(c > 1 for c in bottom_counts.values()):
            multi_rec_per_bottom.append((release_dir, wid, tracks))

    print(f"# Scan of {ROOT}")
    print(f"# files read: {len(files)}  read errors: {read_errors}")
    print(f"# total work-groups: {len(groups)}")
    print(f"# NON-UNIFORM-DEPTH groups: {len(nonuniform)}")
    print(f"# MULTI-RECORDING-PER-BOTTOM-WORK groups: {len(multi_rec_per_bottom)}")
    print()

    print("=" * 100)
    print("NON-UNIFORM HIERARCHY DEPTH (one work-group, mixed CWP_PART_LEVELS)")
    print("=" * 100)
    for release_dir, wid, tracks in sorted(nonuniform):
        depth_hist = Counter(t["part_levels"] for t in tracks)
        work_top = tracks[0]["work_top"]
        print(f"\n### {release_dir}")
        print(f"    work_top   = {work_top!r}")
        print(f"    workid_top = {wid}")
        print(f"    depth histogram (CWP_PART_LEVELS -> n_tracks): {dict(sorted(depth_hist.items()))}")
        # Show the minority-depth tracks (the anomalous ones) with detail.
        majority = depth_hist.most_common(1)[0][0]
        minority = [t for t in tracks if t["part_levels"] != majority]
        print(f"    majority depth = {majority}; {len(minority)} anomalous track(s):")
        for t in sorted(minority, key=lambda x: x["file"]):
            print(f"      [PL={t['part_levels']}] {t['title']!r}")
            print(f"               part_0={t['part_0']!r}  ok0={t['ordering_key_0']}")

    print()
    print("=" * 100)
    print("MULTI-RECORDING-PER-BOTTOM-WORK (leaf-collision driver; independent of depth)")
    print("=" * 100)
    print(f"(count only — {len(multi_rec_per_bottom)} groups; these are where leaf-numbering collides)")
    for release_dir, wid, tracks in sorted(multi_rec_per_bottom):
        bottom_counts = Counter(t["workid_0"] for t in tracks if t["workid_0"])
        worst = max(bottom_counts.values())
        n_colliding_works = sum(1 for c in bottom_counts.values() if c > 1)
        print(f"  {release_dir}  | work_top={tracks[0]['work_top']!r} | "
              f"{n_colliding_works} bottom-work(s) hold >1 rec; max={worst}")


if __name__ == "__main__":
    main()
