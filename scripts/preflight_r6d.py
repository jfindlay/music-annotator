#!/usr/bin/env python3
"""Consolidated dry-run preflight report across all offline maintenance passes.

Runs each of the six offline maintenance passes (:func:`repath`, :func:`regroup`,
:func:`unify`, :func:`enrich`, :func:`repatch_catalogue_colon`,
:func:`repatch_acoustid_tags`) with ``dry_run=True`` over the annotated library root and
assembles a consolidated :class:`~music_annotator.models.PreflightReport` containing:

1. **Per-pass change-set totals**: how many files each pass would act on.
2. **Cross-pass overlap map**: files appearing in more than one pass's plan.  A file in
   multiple plans means the ordering of those passes is load-bearing (tag-content rewrites
   must precede path rewrites so the corrected tags drive the new destination path).
3. **Journal capacity**: the current journal entry count, on-disk file size, and the
   projected entry-count delta if all planned passes were executed.
4. **Reference/ retention evidence**: presence and disk footprint of the ``Reference/``
   snapshot directory alongside the library root.  Evidence only — no automated retention
   decision is made.

Ad-hoc analysis tool, not part of the package.  ``ROOT`` is machine-specific — adjust to
the local library root.

Exit behaviour
--------------
If ``ROOT`` does not exist or is empty (e.g. the remote filesystem is not mounted), the
script exits immediately with a clear "SCAN NOT RUN" message and a non-zero exit code.
This prevents a missing mount from being silently reported as "no findings" — a
data-integrity hazard when the result is used to validate the library population before a
destructive pass runs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from music_annotator._pipeline_io import JOURNAL_FILENAME
from music_annotator._pipeline_maint import compose_preflight_report

ROOT = os.path.expanduser("~/Remote/hades/Music/Done")


def _check_root(root: str) -> None:
    """Verify the library root exists and is non-empty; exit with a clear message if not.

    Distinguishes "scan not run" (root absent or empty) from "scan ran, no findings".
    Exiting here prevents a missing mount from being silently reported as a clean census.

    :param root: Absolute path to the library root directory.
    :raises SystemExit: Always exits when the root is absent or empty.
    """
    if not os.path.isdir(root):
        print(f"SCAN NOT RUN — library root not mounted or does not exist: {root}", file=sys.stderr)
        sys.exit(1)
    try:
        entries = os.listdir(root)
    except PermissionError as exc:
        print(f"SCAN NOT RUN — cannot list root: {exc}", file=sys.stderr)
        sys.exit(1)
    if not entries:
        print(f"SCAN NOT RUN — root is empty (filesystem not mounted?): {root}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """Run the consolidated preflight report and print findings to stdout."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Consolidated dry-run preflight report across all offline maintenance passes.",
    )
    parser.add_argument(
        "--root",
        default=ROOT,
        help="Library root directory (default: %(default)s).",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        metavar="PATH",
        default="",
        help="Also serialise the report to a JSON file at PATH.",
    )
    args = parser.parse_args()

    root = args.root
    _check_root(root)

    dest_root = Path(root)
    journal_path = dest_root / JOURNAL_FILENAME

    report = compose_preflight_report(dest_root, journal_path)

    if not report.scan_ran:
        # _check_root already exited for the common cases; this branch handles the rare
        # race where the root disappears between _check_root and compose_preflight_report.
        print("SCAN NOT RUN — library root not mounted or empty.", file=sys.stderr)
        sys.exit(1)

    # --- Formatted text output ---
    print(f"# Preflight report for {root}")
    print()

    print("=" * 100)
    print("PER-PASS CHANGE-SET TOTALS")
    print("=" * 100)
    for summary in report.pass_summaries:
        overlap_note = f"  ({summary.overlap_count} overlapping)" if summary.overlap_count else ""
        print(f"  {summary.pass_name:<30}  {summary.count:>6} planned{overlap_note}")
    print()

    print("=" * 100)
    print("CROSS-PASS OVERLAP MAP (files in more than one pass's plan)")
    print("=" * 100)
    if report.overlaps:
        for entry in report.overlaps:
            print(f"  {entry.current_path}")
            print(f"    passes: {', '.join(entry.pass_names)}")
    else:
        print("  (none — no file appears in more than one pass's plan)")
    print()

    print("=" * 100)
    print("JOURNAL CAPACITY")
    print("=" * 100)
    cap = report.journal_capacity
    print(f"  Current entry count:      {cap.current_entry_count}")
    print(f"  Current file size:        {cap.current_size_bytes} bytes")
    print(f"  Projected delta entries:  {cap.projected_delta_entries}")
    print(f"  Projected total entries:  {cap.current_entry_count + cap.projected_delta_entries}")
    print()

    print("=" * 100)
    print("REFERENCE/ RETENTION EVIDENCE")
    print("=" * 100)
    ref = report.reference_evidence
    ref_path = dest_root.parent / "Reference"
    if ref.present:
        print(f"  Reference/ directory:  present at {ref_path}")
        print(f"  Disk footprint:        {ref.size_bytes} bytes")
    else:
        print(f"  Reference/ directory:  absent (expected at {ref_path})")
    print()

    print("=" * 100)
    print("CENSUS SUMMARY")
    print("=" * 100)
    total_planned = sum(s.count for s in report.pass_summaries)
    print(f"  Total planned changes:    {total_planned}")
    print(f"  Cross-pass overlap files: {len(report.overlaps)}")
    if total_planned == 0:
        print()
        print("  No changes planned across all passes.")
        print("  Either all passes have already run, or the library is already in the target state.")
    else:
        print()
        print("  The passes above have planned changes.  Review the overlap map before executing")
        print("  any pass for real: tag-content rewrites must precede path rewrites so the")
        print("  corrected tags drive the new destination path.")

    # --- Optional JSON output ---
    if args.output_json:
        json_path = Path(args.output_json)
        json_path.write_text(
            json.dumps(report.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print()
        print(f"Report serialised to: {json_path}")


if __name__ == "__main__":
    main()
