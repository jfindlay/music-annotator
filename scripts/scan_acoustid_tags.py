#!/usr/bin/env python3
"""Scan the annotated library for legacy AcoustID tag state.

Two independent signals are reported:

1. **Legacy fingerprint key**: files still carrying the legacy ``CHROMAPRINT_FP`` Vorbis Comment
   key (FLAC) or TXXX frame with description ``"Chromaprint Fingerprint"`` (MP3).  These are
   candidates for the offline AcoustID repatch pass, which migrates the fingerprint value to the
   Picard-aligned ``ACOUSTID_FINGERPRINT`` key and removes the legacy key.

2. **Empty ``ACOUSTID_ID``**: files where ``ACOUSTID_ID`` is absent or empty — candidates for a
   keyed ``enrich --re-resolve`` run that backfills the AcoustID cluster UUID from the fingerprint
   ``/v2/lookup`` endpoint.

The Picard-aligned convention stores the raw Chromaprint fingerprint under ``ACOUSTID_FINGERPRINT``
(FLAC Vorbis ``acoustid_fingerprint``; MP3 TXXX desc ``"Acoustid Fingerprint"``) and the AcoustID
cluster UUID under ``ACOUSTID_ID`` (sourced exclusively from the fingerprint ``/v2/lookup``
endpoint).  Files carrying the legacy ``CHROMAPRINT_FP`` key are not yet migrated; files with an
empty ``ACOUSTID_ID`` have not yet had a keyed fingerprint lookup performed.

Ad-hoc analysis tool, not part of the package.  Refreshes the census in ``docs/BACKLOG.md``
("AcoustID tag naming + semantics"); re-run it when the library changes to update the count.
``ROOT`` is machine-specific — adjust to the local library root.

Exit behaviour
--------------
If ``ROOT`` does not exist or is empty (e.g. the remote filesystem is not mounted), the script
exits immediately with a clear "SCAN NOT RUN" message and a non-zero exit code.  This prevents a
missing mount from being silently reported as "0 files found" — a data-integrity hazard when the
scan result is used to validate the population before the destructive repatch runs.
"""
from __future__ import annotations

import os
import sys

from mutagen.flac import FLAC
from mutagen.id3 import ID3

ROOT = os.path.expanduser("~/Remote/hades/Music/Done")


def _check_root(root: str) -> None:
    """Verify the library root exists and is non-empty; exit with a clear message if not.

    Distinguishes "scan not run" (root absent or empty) from "scan ran, no findings".
    Exiting here prevents a missing mount from being silently reported as a clean census.

    :param root: Absolute path to the library root directory.
    :raises SystemExit: Always exits when the root is absent or empty.
    """
    if not os.path.isdir(root):
        print(f"SCAN NOT RUN: library root not mounted or does not exist: {root}", file=sys.stderr)
        sys.exit(1)
    try:
        entries = os.listdir(root)
    except PermissionError as exc:
        print(f"SCAN NOT RUN: cannot list root: {exc}", file=sys.stderr)
        sys.exit(1)
    if not entries:
        print(f"SCAN NOT RUN: library root not mounted or empty: {root}", file=sys.stderr)
        sys.exit(1)


def _has_legacy_key_flac(path: str) -> bool:
    """Return ``True`` when the FLAC file at ``path`` carries the legacy ``CHROMAPRINT_FP`` key.

    Reads the Vorbis Comment block directly via mutagen.  The legacy key is ``chromaprint_fp``
    (Vorbis Comment keys are case-insensitive; mutagen stores them lowercase).

    :param path: Absolute path to the FLAC file.
    :returns: ``True`` when the legacy key is present, ``False`` otherwise.
    :raises Exception: On any mutagen read error (caller catches).
    """
    audio = FLAC(path)
    if audio.tags is None:
        return False
    return bool(audio.get("chromaprint_fp") or audio.get("CHROMAPRINT_FP"))


def _has_legacy_key_mp3(path: str) -> bool:
    """Return ``True`` when the MP3 file at ``path`` carries the legacy ``"Chromaprint Fingerprint"`` TXXX frame.

    Reads the ID3 tags directly via mutagen and checks for a TXXX frame whose description is
    ``"Chromaprint Fingerprint"`` — the legacy MP3 fingerprint key.

    :param path: Absolute path to the MP3 file.
    :returns: ``True`` when the legacy TXXX frame is present, ``False`` otherwise.
    :raises Exception: On any mutagen read error (caller catches).
    """
    id3 = ID3(path)  # type: ignore[no-untyped-call]
    for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
        if frame.desc == "Chromaprint Fingerprint":
            return True
    return False


def _acoustid_id_flac(path: str) -> str:
    """Return the ``ACOUSTID_ID`` tag value from the FLAC file at ``path``, or empty string.

    :param path: Absolute path to the FLAC file.
    :returns: The first ``acoustid_id`` tag value, or ``""`` when absent or empty.
    :raises Exception: On any mutagen read error (caller catches).
    """
    audio = FLAC(path)
    vals = audio.get("acoustid_id") or audio.get("ACOUSTID_ID") or []
    return vals[0] if vals else ""


def _acoustid_id_mp3(path: str) -> str:
    """Return the ``ACOUSTID_ID`` TXXX tag value from the MP3 file at ``path``, or empty string.

    Looks for a TXXX frame with description ``"Acoustid Id"`` (the Picard-aligned MP3 key).

    :param path: Absolute path to the MP3 file.
    :returns: The TXXX ``"Acoustid Id"`` value, or ``""`` when absent or empty.
    :raises Exception: On any mutagen read error (caller catches).
    """
    id3 = ID3(path)  # type: ignore[no-untyped-call]
    for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
        if frame.desc == "Acoustid Id":
            return frame.text[0] if frame.text else ""
    return ""


def main() -> None:
    """Run the AcoustID tag state scan and print findings to stdout."""
    _check_root(ROOT)

    # Collect all FLAC and MP3 files under ROOT.
    audio_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        for fname in filenames:
            if fname.lower().endswith((".flac", ".mp3")):
                audio_files.append(os.path.join(dirpath, fname))
        dirnames.sort()
    audio_files.sort()

    files_scanned = 0
    read_errors = 0
    legacy_key_files: list[str] = []
    empty_acoustid_id_files: list[str] = []

    for fpath in audio_files:
        files_scanned += 1
        try:
            if fpath.lower().endswith(".flac"):
                has_legacy = _has_legacy_key_flac(fpath)
                acoustid_id = _acoustid_id_flac(fpath)
            else:
                has_legacy = _has_legacy_key_mp3(fpath)
                acoustid_id = _acoustid_id_mp3(fpath)
        except Exception:  # noqa: BLE001
            read_errors += 1
            continue

        rel = os.path.relpath(fpath, ROOT)
        if has_legacy:
            legacy_key_files.append(rel)
        if not acoustid_id:
            empty_acoustid_id_files.append(rel)

    # Print summary.
    print(f"# Scan of {ROOT}")
    print(f"# files scanned: {files_scanned}  read errors: {read_errors}")
    print(f"# files with legacy CHROMAPRINT_FP key: {len(legacy_key_files)}")
    print(f"# files with empty ACOUSTID_ID: {len(empty_acoustid_id_files)}")
    print()

    print("=" * 100)
    print("FILES WITH LEGACY CHROMAPRINT_FP KEY (candidates for the AcoustID repatch pass)")
    print("=" * 100)
    if legacy_key_files:
        for rel in legacy_key_files:
            print(f"  {rel}")
    else:
        print("  (none found)")

    print()
    print("=" * 100)
    print("FILES WITH EMPTY ACOUSTID_ID (candidates for a keyed enrich --re-resolve run)")
    print("=" * 100)
    if empty_acoustid_id_files:
        for rel in empty_acoustid_id_files[:50]:
            print(f"  {rel}")
        if len(empty_acoustid_id_files) > 50:  # noqa: PLR2004
            print(f"  ... and {len(empty_acoustid_id_files) - 50} more (truncated)")
    else:
        print("  (none found)")

    print()
    print("=" * 100)
    print("CENSUS SUMMARY")
    print("=" * 100)
    print(f"  Files scanned:                    {files_scanned}")
    print(f"  Read errors:                      {read_errors}")
    print(f"  Files with legacy CHROMAPRINT_FP: {len(legacy_key_files)}")
    print(f"  Files with empty ACOUSTID_ID:     {len(empty_acoustid_id_files)}")
    print()
    if not legacy_key_files:
        print("  No legacy CHROMAPRINT_FP keys found.")
        print("  The AcoustID repatch pass has either not yet run (expected until the destructive")
        print("  library-wide repatch executes) or the library is already fully migrated.")
    else:
        print("  Files above with the legacy CHROMAPRINT_FP key are candidates for the offline")
        print("  AcoustID repatch pass, which migrates the fingerprint value to the Picard-aligned")
        print("  ACOUSTID_FINGERPRINT key and removes the legacy key.")
    if empty_acoustid_id_files:
        print()
        print("  Files with empty ACOUSTID_ID can be backfilled by running:")
        print("    music-annotator enrich --re-resolve --acoustid-key <KEY> <dest_root>")


if __name__ == "__main__":
    main()
