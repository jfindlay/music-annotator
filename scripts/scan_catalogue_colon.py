#!/usr/bin/env python3
"""Scan the annotated library for corrupt catalogue-colon CWP_PART labels and NN - NN directories.

A ``CWP_PART_{i}`` tag is corrupt when the old bare-``":"`` split in ``strip_common_prefix``
truncated a catalogue number (e.g. Haydn Hoboken ``"Hob. III:31"``) to a bare fragment (``"31"``),
minting intermediate directories named ``01 - 31``, ``02 - 32``, etc.  The forward fix keys on
``": "`` (colon followed by space) so new ingests are correct; this scanner identifies releases
already on disk that carry the old corruption.

Detection uses :func:`music_annotator._works.is_catalogue_colon_corrupt` — the same predicate the
offline repatch pass applies — so the scan and the repatch are guaranteed to agree on which files
are corrupt.  The corrected label is shown alongside the stored corrupt label so the operator can
verify the recomputation before the destructive repatch runs.

Two independent signals are reported:

1. **Tag-content corruption**: files whose ``CWP_PART_{i}`` disagrees with the recomputed label
   under the ``": "`` rule and carries the catalogue-colon signature.
2. **NN - NN directories**: directories whose name matches ``r"^\\d{2} - \\d{2}$"`` — the
   path-level symptom of the same bug (a bare two-digit catalogue fragment used as an intermediate
   directory label).

Ad-hoc analysis tool, not part of the package.  Refreshes the census in ``docs/BACKLOG.md``
("Catalogue-colon part-label retro-fix"); re-run it when the library changes to update the count.
``ROOT`` is machine-specific — adjust to the local library root.

Exit behaviour
--------------
If ``ROOT`` does not exist or is empty (e.g. the remote filesystem is not mounted), the script
exits immediately with a clear "SCAN NOT RUN — library root not mounted" message and a non-zero
exit code.  This prevents a missing mount from being silently reported as "no findings" — a
data-integrity hazard when the scan result is used to validate the population before the
destructive repatch runs.
"""
from __future__ import annotations

import os
import re
import sys

from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from music_annotator._works import CANNOT_RECOMPUTE, is_catalogue_colon_corrupt, rederive_part_label

ROOT = os.path.expanduser("~/Remote/hades/Music/Done")

# Pattern for NN - NN intermediate directories (two-digit number, space-dash-space, two-digit number).
_NN_NN_RE = re.compile(r"^\d{2} - \d{2}$")


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


def _tag_value(tags: dict[str, list[str]], key: str) -> str:
    """Return the first value for ``key`` from a tag dict, or empty string.

    :param tags: Tag dict mapping key to list of string values.
    :param key: Tag key to look up (case-sensitive).
    :returns: First tag value, or ``""`` when absent or empty.
    """
    vals = tags.get(key)
    return vals[0] if vals else ""


def _read_flac_tags(path: str) -> dict[str, list[str]]:
    """Read FLAC tags from ``path`` and return as a dict of key → list[str].

    :param path: Absolute path to the FLAC file.
    :returns: Tag dict with uppercase keys.
    :raises Exception: On any mutagen read error.
    """
    audio = FLAC(path)
    return {k.upper(): list(v) for k, v in audio.tags.items()} if audio.tags else {}


def _read_mp3_tags(path: str) -> dict[str, list[str]]:
    """Read MP3 TXXX tags from ``path`` and return as a dict of key → list[str].

    Only TXXX (user-defined text) frames are read, since CWP_* tags are stored as TXXX.

    :param path: Absolute path to the MP3 file.
    :returns: Tag dict with uppercase keys (TXXX description used as key).
    :raises Exception: On any mutagen read error.
    """
    audio = MP3(path)
    result: dict[str, list[str]] = {}
    if audio.tags is None:
        return result
    for frame in audio.tags.values():
        if frame.FrameID == "TXXX":
            key = frame.desc.upper()
            result[key] = list(frame.text)
    return result


def _scan_file_for_corrupt_parts(path: str, tags: dict[str, list[str]]) -> list[tuple[int, str, str]]:
    """Check all CWP_PART_{i} levels in ``tags`` for catalogue-colon corruption.

    For each level ``i`` where ``CWP_PART_{i}`` is present, applies
    :func:`~music_annotator._works.is_catalogue_colon_corrupt` using the embedded
    ``CWP_WORK_{i}`` / ``CWP_WORK_{i+1}`` pair.  Returns findings as a list of
    ``(level, stored_label, corrected_label)`` tuples.

    :param path: File path (used only for logging context; not read here).
    :param tags: Tag dict with uppercase keys.
    :returns: List of ``(level_index, stored_corrupt_label, corrected_label)`` for each corrupt level.
    """
    findings: list[tuple[int, str, str]] = []
    i = 0
    while True:
        part_key = f"CWP_PART_{i}"
        work_key = f"CWP_WORK_{i}"
        parent_key = f"CWP_WORK_{i + 1}"
        stored = _tag_value(tags, part_key)
        if not stored and not _tag_value(tags, work_key):
            # No more levels.
            break
        child_title = _tag_value(tags, work_key)
        parent_title = _tag_value(tags, parent_key)
        if stored and is_catalogue_colon_corrupt(stored, child_title, parent_title):
            recomputed = rederive_part_label(child_title, parent_title)
            corrected = recomputed if recomputed is not CANNOT_RECOMPUTE else stored
            findings.append((i, stored, str(corrected)))
        i += 1
        if i > 20:
            # Guard against pathological tag data with no termination.
            break
    return findings


def main() -> None:
    """Run the catalogue-colon corruption scan and print findings to stdout."""
    _check_root(ROOT)

    # Collect all FLAC and MP3 files under ROOT.
    audio_files: list[str] = []
    nn_nn_dirs: list[str] = []

    for dirpath, dirnames, filenames in os.walk(ROOT):
        # Check each directory name for the NN - NN pattern.
        rel_dir = os.path.relpath(dirpath, ROOT)
        dirname = os.path.basename(dirpath)
        if _NN_NN_RE.match(dirname):
            nn_nn_dirs.append(rel_dir)
        # Collect audio files.
        for fname in filenames:
            if fname.lower().endswith((".flac", ".mp3")):
                audio_files.append(os.path.join(dirpath, fname))
        # Sort subdirectory traversal order for deterministic output.
        dirnames.sort()

    audio_files.sort()

    # Scan each file for corrupt CWP_PART tags.
    files_scanned = 0
    read_errors = 0
    corrupt_files: list[tuple[str, list[tuple[int, str, str]]]] = []

    for fpath in audio_files:
        files_scanned += 1
        try:
            if fpath.lower().endswith(".flac"):
                tags = _read_flac_tags(fpath)
            else:
                tags = _read_mp3_tags(fpath)
        except Exception:  # noqa: BLE001
            read_errors += 1
            continue
        findings = _scan_file_for_corrupt_parts(fpath, tags)
        if findings:
            rel = os.path.relpath(fpath, ROOT)
            corrupt_files.append((rel, findings))

    # Print summary.
    print(f"# Scan of {ROOT}")
    print(f"# files scanned: {files_scanned}  read errors: {read_errors}")
    print(f"# files with corrupt CWP_PART tags: {len(corrupt_files)}")
    print(f"# NN - NN directories found: {len(nn_nn_dirs)}")
    print()

    print("=" * 100)
    print("CORRUPT CWP_PART TAGS (catalogue-colon signature)")
    print("=" * 100)
    if corrupt_files:
        for rel, findings in corrupt_files:
            print(f"\n  {rel}")
            for level, stored, corrected in findings:
                print(f"    CWP_PART_{level}: stored={stored!r}  →  corrected={corrected!r}")
    else:
        print("  (none found)")

    print()
    print("=" * 100)
    print("NN - NN INTERMEDIATE DIRECTORIES (path-level symptom)")
    print("=" * 100)
    if nn_nn_dirs:
        for d in sorted(nn_nn_dirs):
            print(f"  {d}")
    else:
        print("  (none found)")

    print()
    print("=" * 100)
    print("CENSUS SUMMARY")
    print("=" * 100)
    print(f"  Files scanned:                {files_scanned}")
    print(f"  Files with corrupt tags:      {len(corrupt_files)}")
    print(f"  NN - NN directories:          {len(nn_nn_dirs)}")
    if not corrupt_files and not nn_nn_dirs:
        print()
        print("  No catalogue-colon corruption found in the current library.")
        print("  The offline repatch pass has either not yet run (expected until the destructive")
        print("  library-wide repatch executes) or the library is already clean.")
    else:
        print()
        print("  Corrupt files above are candidates for the offline repatch pass.")
        print("  The repatch corrects CWP_PART_* and CWP_GROUPHEADING tags in-place;")
        print("  a subsequent repath pass renders the corrected directory names.")


if __name__ == "__main__":
    main()
