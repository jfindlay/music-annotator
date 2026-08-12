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
the depth-normalisation work reopens to refresh the shape distribution against a more
complete library.  ``ROOT`` is machine-specific — adjust to the local library root.

Exit behaviour
--------------
If ``ROOT`` does not exist or is empty (e.g. the remote filesystem is not mounted), the
script exits immediately with a clear "scan not run — root not mounted" message and a
non-zero exit code.  This prevents a missing mount from being silently reported as
"no findings" — a data-integrity hazard when the scan result is used to validate the
depth-shape taxonomy.

JSON artifact
-------------
Pass ``--json <path>`` to emit a machine-readable summary alongside the human-readable
output.  The artifact records the scan timestamp, file/group counts, and the list of
non-uniform groups (each with release_dir, workid_top, work_top, and depth histogram).
This eases downstream consumers that need to compare census snapshots across library
states without re-parsing the human-readable output.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from mutagen.flac import FLAC

ROOT = os.path.expanduser("~/Remote/hades/Music/Done")


def _check_root(root: str) -> None:
    """Verify the library root exists and is non-empty; exit with a clear message if not.

    Distinguishes "scan not run" (root absent or empty) from "scan ran, no findings".
    Exiting here prevents a missing mount from being silently reported as a clean census.

    :param root: Absolute path to the library root directory.
    :raises SystemExit: Always exits when the root is absent or empty.
    """
    if not os.path.isdir(root):
        print(f"scan not run — root not mounted or does not exist: {root}", file=sys.stderr)
        sys.exit(1)
    # Check for at least one entry (mounted but empty = same hazard as not mounted).
    try:
        entries = os.listdir(root)
    except PermissionError as exc:
        print(f"scan not run — cannot list root: {exc}", file=sys.stderr)
        sys.exit(1)
    if not entries:
        print(f"scan not run — root is empty (filesystem not mounted?): {root}", file=sys.stderr)
        sys.exit(1)


def g(audio: FLAC, key: str) -> str:
    """Return the first value for ``key`` from a FLAC tag dict, or empty string.

    :param audio: Mutagen FLAC object.
    :param key: Tag key to look up.
    :returns: First tag value, or ``""`` when absent.
    """
    v = audio.get(key)
    return v[0] if v else ""


def main() -> None:
    """Run the non-uniform-depth scan and print findings to stdout."""
    parser = argparse.ArgumentParser(description="Scan library for non-uniform CWP_PART_LEVELS within work-groups.")
    parser.add_argument(
        "--json",
        metavar="PATH",
        default=None,
        help="Write a machine-readable JSON summary to PATH alongside the human-readable output.",
    )
    args = parser.parse_args()

    _check_root(ROOT)

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

    if args.json is not None:
        _write_json(args.json, ROOT, files, read_errors, groups, nonuniform)


def _write_json(
    path: str,
    root: str,
    files: list[str],
    read_errors: int,
    groups: dict[tuple[str, str], list[dict[str, str]]],
    nonuniform: list[tuple[str, str, list[dict[str, str]]]],
) -> None:
    """Write a machine-readable JSON summary of the scan to ``path``.

    The artifact records the scan timestamp, file/group counts, and the list of non-uniform
    groups (each with release_dir, workid_top, work_top, and depth histogram).  Intended to
    ease downstream consumers that compare census snapshots across library states without
    re-parsing the human-readable output.

    :param path: Filesystem path to write the JSON file.
    :param root: Library root that was scanned.
    :param files: List of FLAC file paths found.
    :param read_errors: Number of files that could not be read.
    :param groups: All work-groups found (keyed by (release_dir, workid_top)).
    :param nonuniform: Subset of groups with non-uniform CWP_PART_LEVELS.
    """
    nonuniform_records = []
    for release_dir, wid, tracks in sorted(nonuniform):
        depth_hist = Counter(t["part_levels"] for t in tracks)
        nonuniform_records.append(
            {
                "release_dir": release_dir,
                "workid_top": wid,
                "work_top": tracks[0]["work_top"],
                "depth_histogram": dict(sorted(depth_hist.items())),
                "track_count": len(tracks),
            }
        )
    artifact = {
        "scan_root": root,
        "scanned_at": datetime.now(tz=timezone.utc).isoformat(),
        "files_read": len(files),
        "read_errors": read_errors,
        "total_work_groups": len(groups),
        "nonuniform_depth_groups": len(nonuniform),
        "nonuniform": nonuniform_records,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, indent=2)
    print(f"\n# JSON artifact written to: {path}")


if __name__ == "__main__":
    main()
