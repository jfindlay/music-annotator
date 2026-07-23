#!/usr/bin/env python3
"""Scan the annotated library for cross-medium fragmentation shapes not caught by the release-keyed detector.

Implements three residual-shape passes, each grouping embedded tags by a **different join key** than
the existing ``_audit.detect_fragmented_releases`` detector (which keys on ``MUSICBRAINZ_ALBUMID``):

1. **rg-multi-release (box set):** group audio files by ``MUSICBRAINZ_RELEASEGROUPID``; flag
   release-groups whose files carry ≥2 distinct ``MUSICBRAINZ_ALBUMID`` **and** span ≥2 top_dirs.
   This is the shape ``_audit.detect_fragmented_releases`` misses because it keys on album, not RG.

2. **per-medium-credit-variance:** within one ``MUSICBRAINZ_ALBUMID``, compare the rendered
   ``ALBUMARTIST`` across media (disc subdirs); flag releases whose media disagree on the credit
   that drives the top_dir/work_dir path.

3. **rg-vs-release-split:** flag where the same ``MUSICBRAINZ_RELEASEGROUPID`` appears in files
   with different ``ALBUMARTIST`` values across different top_dirs.  This captures the case where
   attribution keyed on release vs release-group would place the same conceptual work in different
   paths — the RG-level credit diverges from the release-level credit, so a RG-keyed path and a
   release-keyed path would disagree.  Operationally: a RG whose files span ≥2 top_dirs **and**
   whose files carry ≥2 distinct ``ALBUMARTIST`` values (the credit that drives top_dir placement).
   Distinct from rg-multi-release (which keys on album-count, not artist-credit divergence).

**C-FRAG-TAX — fragmentation-shape taxonomy (frozen at S1).**

Shape vocabulary (enumerated set — all three carried even at zero live instances):

- ``rg-multi-release``: one release-group, ≥2 distinct album MBIDs, spanning ≥2 top_dirs.
- ``per-medium-credit-variance``: one album, ≥2 media (disc subdirs), ≥2 distinct ALBUMARTIST values.
- ``rg-vs-release-split``: one release-group, ≥2 top_dirs, ≥2 distinct ALBUMARTIST values.

JSON record schema per finding::

    {
        "shape": "rg-multi-release" | "per-medium-credit-variance" | "rg-vs-release-split",
        "release_group_id": "<mbid or empty for per-medium-credit-variance>",
        "album_ids": ["<mbid>", ...],
        "top_dirs": ["<name>", ...],
        "files": ["<path>", ...],
        "remedy_route": "undetermined"   # filled by S2; frozen field, deferred value
    }

**Join-key authority (C-W2):** the join key is the embedded tag, not the journal.  Files whose
relevant tag cannot be read or is empty are silently skipped.

**Standalone posture:** this script does not import from ``src/music_annotator/``.  It uses
``mutagen`` directly (available in the project venv).  Tag-reading follows the same pattern as
``_pipeline_io._read_albumid_tag`` and ``_audit.detect_fragmented_releases``.

**Host-path caveat (D-1):** ``ROOT`` is machine-specific.  A mismatched or unmounted root reads
zero files.  The scanner prints ``files read: N`` up front and refuses to emit an "empty = clean"
census — an unreadable/empty root is reported as *scan-not-run*, never as *no fragmentation*.

Ad-hoc analysis tool, not part of the package.  ``ROOT`` is machine-specific — adjust to the local
library root.  Produces ``docs/census-fragmentation.{md,json}`` when run against the live library
(S2 deliverable).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TXXX  # type: ignore[attr-defined]

ROOT = os.path.expanduser("~/Remote/hades/Music/Done")

# ---------------------------------------------------------------------------
# Tag-reading helpers (standalone; mirrors _pipeline_io pattern without importing src/)
# ---------------------------------------------------------------------------

#: TXXX frame description strings for the MB tags we need.
#: Mirrors _tagger._MP3_TXXX_MAP for the relevant subset.
_MP3_TXXX_DESCS: dict[str, str] = {
    "MUSICBRAINZ_ALBUMID": "MusicBrainz Album Id",
    "MUSICBRAINZ_RELEASEGROUPID": "MusicBrainz Release Group Id",
}


def _read_flac_tags(path: str) -> dict[str, str]:
    """Read Vorbis Comment tags from a FLAC file, returning an uppercased key dict.

    Only the first value of each multi-valued comment is returned.  Returns an empty dict on any
    read error.

    :param path: Absolute path to the FLAC file.
    :returns: ``{UPPERCASE_KEY: value}`` dict of non-empty tag values.
    """
    try:
        audio = FLAC(path)
        return {k.upper(): v[0] for k, v in audio.items() if v and v[0]}
    except Exception:  # noqa: BLE001 — best-effort; any failure means skip
        return {}


def _read_mp3_tags(path: str) -> dict[str, str]:
    """Read ID3 tags from an MP3 file, returning an uppercased key dict for the tags we need.

    Reads ``TPE2`` (ALBUMARTIST), ``TPOS`` (DISCNUMBER), and the TXXX frames for
    ``MUSICBRAINZ_ALBUMID`` and ``MUSICBRAINZ_RELEASEGROUPID``.  Returns an empty dict on any
    read error.

    :param path: Absolute path to the MP3 file.
    :returns: ``{UPPERCASE_KEY: value}`` dict of non-empty tag values.
    """
    try:
        id3 = ID3(path)  # type: ignore[no-untyped-call]
        result: dict[str, str] = {}

        # Standard text frames we need
        std_map: dict[str, str] = {
            "TPE2": "ALBUMARTIST",
            "TPOS": "DISCNUMBER",
        }
        for frame_id, tag_key in std_map.items():
            frame = id3.get(frame_id)  # type: ignore[no-untyped-call]
            if frame and str(frame):
                result[tag_key] = str(frame)

        # TXXX frames for MB IDs
        inv_descs: dict[str, str] = {v: k for k, v in _MP3_TXXX_DESCS.items()}
        for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
            tag_key_maybe: str | None = inv_descs.get(frame.desc)
            if tag_key_maybe and frame.text and frame.text[0]:
                result[tag_key_maybe] = frame.text[0]

        return {k: v for k, v in result.items() if v}
    except Exception:  # noqa: BLE001 — best-effort; any failure means skip
        return {}


def _read_tags(path: str) -> dict[str, str]:
    """Dispatch tag reading to the FLAC or MP3 reader based on file extension.

    :param path: Absolute path to the audio file.
    :returns: ``{UPPERCASE_KEY: value}`` dict, or empty dict for unsupported formats or read errors.
    """
    ext = os.path.splitext(path)[1].lower()
    match ext:
        case ".flac":
            return _read_flac_tags(path)
        case ".mp3":
            return _read_mp3_tags(path)
        case _:
            return {}


# ---------------------------------------------------------------------------
# File-info dataclass (plain dict to avoid Any-typed dataclass fields)
# ---------------------------------------------------------------------------


def _collect_files(root: str) -> list[dict[str, str]]:
    """Walk ``root`` recursively and collect tag data from every FLAC and MP3 file.

    For each file, reads: ``MUSICBRAINZ_RELEASEGROUPID``, ``MUSICBRAINZ_ALBUMID``,
    ``ALBUMARTIST``, ``DISCNUMBER``, plus the file path and its top_dir (first path component
    under ``root``).

    Files whose tags cannot be read are silently skipped (tag-is-authority posture: a file with
    no readable tags contributes no evidence).

    :param root: Absolute path to the library root.
    :returns: List of per-file dicts with keys ``path``, ``top_dir``, ``rg_id``, ``album_id``,
        ``albumartist``, ``discnumber``.
    """
    pattern_flac = os.path.join(root, "**", "*.flac")
    pattern_mp3 = os.path.join(root, "**", "*.mp3")
    all_files = sorted(glob.glob(pattern_flac, recursive=True) + glob.glob(pattern_mp3, recursive=True))

    records: list[dict[str, str]] = []
    for fpath in all_files:
        tags = _read_tags(fpath)
        rel = os.path.relpath(fpath, root)
        parts = rel.split(os.sep)
        top_dir = parts[0] if parts else ""
        records.append(
            {
                "path": fpath,
                "top_dir": top_dir,
                "rg_id": tags.get("MUSICBRAINZ_RELEASEGROUPID", ""),
                "album_id": tags.get("MUSICBRAINZ_ALBUMID", ""),
                "albumartist": tags.get("ALBUMARTIST", ""),
                "discnumber": tags.get("DISCNUMBER", ""),
            }
        )
    return records


# ---------------------------------------------------------------------------
# Shape-detection passes
# ---------------------------------------------------------------------------


def _detect_rg_multi_release(records: list[dict[str, str]]) -> list[dict[str, object]]:
    """Detect release-groups whose files span ≥2 distinct album MBIDs **and** ≥2 top_dirs.

    Groups files by ``MUSICBRAINZ_RELEASEGROUPID``.  A release-group is flagged when it contains
    ≥2 distinct ``MUSICBRAINZ_ALBUMID`` values **and** its files span ≥2 distinct top_dirs.  This
    is the box-set shape that ``_audit.detect_fragmented_releases`` misses because that detector
    keys on album MBID, not release-group MBID.

    Files with an empty ``rg_id`` or ``album_id`` are skipped (tag-is-authority: no evidence).

    :param records: Per-file dicts as returned by :func:`_collect_files`.
    :returns: List of finding dicts conforming to C-FRAG-TAX schema.
    """
    # rg_id -> list of file records
    rg_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for rec in records:
        if rec["rg_id"] and rec["album_id"]:
            rg_groups[rec["rg_id"]].append(rec)

    findings: list[dict[str, object]] = []
    for rg_id, group in sorted(rg_groups.items()):
        album_ids = sorted({r["album_id"] for r in group})
        top_dirs = sorted({r["top_dir"] for r in group})
        if len(album_ids) >= 2 and len(top_dirs) >= 2:  # noqa: PLR2004 — thresholds are the definition
            findings.append(
                {
                    "shape": "rg-multi-release",
                    "release_group_id": rg_id,
                    "album_ids": album_ids,
                    "top_dirs": top_dirs,
                    "files": sorted(r["path"] for r in group),
                    "remedy_route": "undetermined",
                }
            )
    return findings


def _detect_per_medium_credit_variance(records: list[dict[str, str]]) -> list[dict[str, object]]:
    """Detect releases whose media (disc subdirs) carry different ALBUMARTIST values within a single top_dir.

    Groups files by ``(MUSICBRAINZ_ALBUMID, top_dir)``, then within each group compares the
    ``ALBUMARTIST`` across disc subdirs.  A release is flagged when ≥2 distinct ``ALBUMARTIST``
    values appear across its media **within the same top_dir**.

    The within-top_dir constraint is deliberate: if the same album MBID appears in multiple
    top_dirs with different credits, that is the ``rg-vs-release-split`` shape (cross-top_dir
    attribution divergence), not per-medium credit variance.  Per-medium credit variance is
    specifically the case where a single release's disc subdirs carry different credits — a
    within-release inconsistency that would cause the path-builder to produce different paths for
    different discs of the same release.

    Files with an empty ``album_id`` or ``albumartist`` are skipped.  Files with an empty
    ``discnumber`` are treated as disc "1" for grouping purposes (single-disc releases without an
    explicit disc number tag).

    :param records: Per-file dicts as returned by :func:`_collect_files`.
    :returns: List of finding dicts conforming to C-FRAG-TAX schema.
    """
    # (album_id, top_dir) -> list of file records
    album_topdir_groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for rec in records:
        if rec["album_id"] and rec["albumartist"]:
            album_topdir_groups[(rec["album_id"], rec["top_dir"])].append(rec)

    findings: list[dict[str, object]] = []
    for (album_id, top_dir), group in sorted(album_topdir_groups.items()):
        # Collect the set of ALBUMARTIST values per disc within this top_dir
        disc_to_artists: dict[str, set[str]] = defaultdict(set)
        for rec in group:
            disc = rec["discnumber"] or "1"
            disc_to_artists[disc].add(rec["albumartist"])

        # Flatten to the set of all distinct ALBUMARTIST values across all discs in this top_dir
        all_artists = {artist for artists in disc_to_artists.values() for artist in artists}
        if len(all_artists) >= 2:  # noqa: PLR2004 — threshold is the definition
            rg_id = next((r["rg_id"] for r in group if r["rg_id"]), "")
            findings.append(
                {
                    "shape": "per-medium-credit-variance",
                    "release_group_id": rg_id,
                    "album_ids": [album_id],
                    "top_dirs": [top_dir],
                    "files": sorted(r["path"] for r in group),
                    "remedy_route": "undetermined",
                }
            )
    return findings


def _detect_rg_vs_release_split(records: list[dict[str, str]]) -> list[dict[str, object]]:
    """Detect release-groups whose files span ≥2 top_dirs **and** carry ≥2 distinct ALBUMARTIST values.

    This shape captures the case where attribution keyed on release vs release-group would place
    the same conceptual work in different paths.  Operationally: a release-group whose files span
    ≥2 top_dirs **and** whose files carry ≥2 distinct ``ALBUMARTIST`` values (the credit that
    drives top_dir placement).

    This is distinct from ``rg-multi-release`` (which keys on album-count divergence) — a
    release-group can have multiple albums all under the same artist credit (no split), or a single
    album whose release-level credit differs from the RG-level credit (a split with only one album
    MBID).  The discriminating signal here is the ``ALBUMARTIST`` divergence, not the album count.

    Files with an empty ``rg_id`` or ``albumartist`` are skipped.

    :param records: Per-file dicts as returned by :func:`_collect_files`.
    :returns: List of finding dicts conforming to C-FRAG-TAX schema.
    """
    # rg_id -> list of file records
    rg_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for rec in records:
        if rec["rg_id"] and rec["albumartist"]:
            rg_groups[rec["rg_id"]].append(rec)

    findings: list[dict[str, object]] = []
    for rg_id, group in sorted(rg_groups.items()):
        top_dirs = sorted({r["top_dir"] for r in group})
        all_artists = sorted({r["albumartist"] for r in group})
        if len(top_dirs) >= 2 and len(all_artists) >= 2:  # noqa: PLR2004 — thresholds are the definition
            album_ids = sorted({r["album_id"] for r in group if r["album_id"]})
            findings.append(
                {
                    "shape": "rg-vs-release-split",
                    "release_group_id": rg_id,
                    "album_ids": album_ids,
                    "top_dirs": top_dirs,
                    "files": sorted(r["path"] for r in group),
                    "remedy_route": "undetermined",
                }
            )
    return findings


# ---------------------------------------------------------------------------
# Deduplication: rg-vs-release-split vs rg-multi-release overlap
# ---------------------------------------------------------------------------


def _deduplicate_rg_vs_release(
    rg_multi: list[dict[str, object]],
    rg_vs_release: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Remove rg-vs-release-split findings that are strict subsets of rg-multi-release findings.

    A rg-vs-release-split finding is suppressed when its ``release_group_id`` already appears in
    the rg-multi-release findings **and** the rg-multi-release finding already captures the
    artist-credit divergence (i.e. the rg-multi-release finding spans ≥2 top_dirs with ≥2 distinct
    ALBUMARTIST values — meaning the rg-multi-release finding is the more specific shape).

    When the rg-vs-release-split finding is *not* a subset (e.g. the RG has only one album MBID
    but two different ALBUMARTIST values), it is retained as a distinct finding.

    :param rg_multi: Findings from :func:`_detect_rg_multi_release`.
    :param rg_vs_release: Findings from :func:`_detect_rg_vs_release_split`.
    :returns: Filtered list of rg-vs-release-split findings with subsets removed.
    """
    rg_multi_ids: set[str] = {str(f["release_group_id"]) for f in rg_multi}
    result: list[dict[str, object]] = []
    for finding in rg_vs_release:
        rg_id = str(finding["release_group_id"])
        if rg_id in rg_multi_ids:
            # Only suppress if the rg-multi-release finding already covers the artist-credit split.
            # The rg-multi-release finding spans ≥2 top_dirs by definition; if the RG also has
            # ≥2 album IDs, the rg-multi-release shape is the primary finding.  Suppress the
            # rg-vs-release-split to avoid double-counting.
            continue
        result.append(finding)
    return result


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _build_output(
    findings: list[dict[str, object]],
    files_read: int,
    scan_status: str,
) -> dict[str, object]:
    """Build the top-level JSON output dict.

    :param findings: All findings from all three shape passes.
    :param files_read: Total number of audio files read (including those with unreadable tags).
    :param scan_status: ``"ok"`` when the root was scanned successfully; ``"scan-not-run"`` when
        the root was unreadable or empty.
    :returns: Top-level output dict with ``findings`` and ``summary`` keys.
    """
    n_rg_multi = sum(1 for f in findings if f["shape"] == "rg-multi-release")
    n_credit = sum(1 for f in findings if f["shape"] == "per-medium-credit-variance")
    n_split = sum(1 for f in findings if f["shape"] == "rg-vs-release-split")
    return {
        "findings": findings,
        "summary": {
            "rg_multi_release": n_rg_multi,
            "per_medium_credit_variance": n_credit,
            "rg_vs_release_split": n_split,
            "files_read": files_read,
            "scan_status": scan_status,
        },
    }


def _print_human_summary(output: dict[str, object]) -> None:
    """Print a human-readable summary of the scan results to stdout.

    :param output: Top-level output dict as returned by :func:`_build_output`.
    """
    summary = output["summary"]
    assert isinstance(summary, dict)
    print(f"# Fragmentation scan of {ROOT}")
    print(f"# scan_status: {summary['scan_status']}")
    print(f"# files read: {summary['files_read']}")
    print(f"# rg-multi-release findings:          {summary['rg_multi_release']}")
    print(f"# per-medium-credit-variance findings: {summary['per_medium_credit_variance']}")
    print(f"# rg-vs-release-split findings:        {summary['rg_vs_release_split']}")
    print()

    findings = output["findings"]
    assert isinstance(findings, list)
    if not findings:
        print("No fragmentation findings.")
        return

    for finding in findings:
        assert isinstance(finding, dict)
        print(f"  shape:            {finding['shape']}")
        print(f"  release_group_id: {finding['release_group_id']}")
        print(f"  album_ids:        {finding['album_ids']}")
        print(f"  top_dirs:         {finding['top_dirs']}")
        files_list = finding["files"]
        assert isinstance(files_list, list)
        print(f"  files ({len(files_list)}):")
        for fpath in files_list:
            print(f"    {fpath}")
        print(f"  remedy_route:     {finding['remedy_route']}")
        print()


# ---------------------------------------------------------------------------
# Live scan
# ---------------------------------------------------------------------------


def scan(root: str) -> dict[str, object]:
    """Run all three fragmentation-shape passes against ``root`` and return the JSON output dict.

    Prints ``files read: N`` before returning so the caller can detect the silent-no-op hazard
    (D-1): if ``root`` is not mounted or does not exist, ``files_read`` will be 0 and
    ``scan_status`` will be ``"scan-not-run"``.

    :param root: Absolute path to the annotated library root.
    :returns: Top-level output dict as returned by :func:`_build_output`.
    """
    if not os.path.isdir(root):
        print(f"ERROR: ROOT does not exist or is not a directory: {root}", file=sys.stderr)
        print("files read: 0  (scan-not-run)", file=sys.stderr)
        return _build_output([], 0, "scan-not-run")

    records = _collect_files(root)
    files_read = len(records)
    print(f"files read: {files_read}", file=sys.stderr)

    if files_read == 0:
        print(
            "WARNING: No audio files found under ROOT — root may be unmounted or wrong path.",
            file=sys.stderr,
        )
        print("Reporting scan-not-run, not no-fragmentation.", file=sys.stderr)
        return _build_output([], 0, "scan-not-run")

    rg_multi = _detect_rg_multi_release(records)
    credit_variance = _detect_per_medium_credit_variance(records)
    rg_vs_release_raw = _detect_rg_vs_release_split(records)
    rg_vs_release = _deduplicate_rg_vs_release(rg_multi, rg_vs_release_raw)

    all_findings: list[dict[str, object]] = rg_multi + credit_variance + rg_vs_release
    return _build_output(all_findings, files_read, "ok")


# ---------------------------------------------------------------------------
# Fixture / KAT test
# ---------------------------------------------------------------------------

# Minimal valid FLAC bytes (magic + STREAMINFO block, last-metadata bit set).
# Copied from tests/conftest.py — standalone so the scanner has no src/ dependency.
_MINIMAL_FLAC: bytes = (
    b"fLaC"
    b"\x80\x00\x00\x22"  # block header: last=1, type=0, length=34
    b"\x10\x00\x10\x00"  # min_blocksize=4096, max_blocksize=4096
    b"\x00\x00\x00"  # min_framesize=0
    b"\x00\x00\x00"  # max_framesize=0
    b"\x0a\xc4\x42\xf0\x00\x00\x00\x00"  # 44100 Hz, 2ch, 16-bit, 0 samples
    b"\x00" * 16  # MD5
)


def _write_flac_stub(path: str, tags: dict[str, str]) -> None:
    """Write a minimal FLAC stub with the given Vorbis Comment tags.

    Creates parent directories as needed.

    :param path: Absolute path to write the FLAC file to.
    :param tags: ``{tag_name: value}`` dict of Vorbis Comment tags to embed.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(_MINIMAL_FLAC)
    audio = FLAC(path)
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def run_fixture_test() -> int:
    """Run the KAT fixture test: construct a synthetic library and assert the scanner finds exactly the right shapes.

    Constructs a temporary directory with six FLAC stubs exhibiting exactly one instance of each
    of the three fragmentation shapes plus one clean release.  Asserts:

    - Exactly one ``rg-multi-release`` finding (ArtistA + ArtistB, same RG, different album IDs,
      different top_dirs).
    - Exactly one ``per-medium-credit-variance`` finding (ArtistC, same album, two discs with
      different ALBUMARTIST values).
    - Exactly one ``rg-vs-release-split`` finding (ArtistD, same RG, two top_dirs, different
      ALBUMARTIST values, only one album MBID — so rg-multi-release does NOT fire).
    - Zero findings for the clean release (ArtistE).
    - Total ``files_read`` == 6.
    - ``scan_status`` == ``"ok"``.

    Exits with code 0 on pass, non-zero on failure.

    :returns: Exit code (0 = pass, 1 = failure).
    """
    rg_a = "rg-aaaa-0000-0000-0000-000000000001"
    rg_b = "rg-bbbb-0000-0000-0000-000000000002"
    rg_c = "rg-cccc-0000-0000-0000-000000000003"
    rg_d = "rg-dddd-0000-0000-0000-000000000004"
    rg_e = "rg-eeee-0000-0000-0000-000000000005"

    album_a1 = "al-aaaa-0001-0000-0000-000000000001"  # rg_a, disc in ArtistA
    album_a2 = "al-aaaa-0002-0000-0000-000000000002"  # rg_a, disc in ArtistB (box-set split)
    album_c = "al-cccc-0000-0000-0000-000000000003"   # rg_c, two discs, different albumartist
    album_d = "al-dddd-0000-0000-0000-000000000004"   # rg_d, one album, two top_dirs, diff artist
    album_e = "al-eeee-0000-0000-0000-000000000005"   # rg_e, clean

    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        # --- Shape 1: rg-multi-release ---
        # ArtistA/WorkA [rel 2000]/disc1/track1.flac — rg_a, album_a1
        _write_flac_stub(
            os.path.join(tmpdir, "ArtistA", "WorkA [rel 2000]", "disc1", "track1.flac"),
            {
                "MUSICBRAINZ_RELEASEGROUPID": rg_a,
                "MUSICBRAINZ_ALBUMID": album_a1,
                "ALBUMARTIST": "Artist A",
                "DISCNUMBER": "1",
            },
        )
        # ArtistB/WorkB [rel 2001]/disc1/track1.flac — rg_a, album_a2 (same RG, different album, different top_dir)
        _write_flac_stub(
            os.path.join(tmpdir, "ArtistB", "WorkB [rel 2001]", "disc1", "track1.flac"),
            {
                "MUSICBRAINZ_RELEASEGROUPID": rg_a,
                "MUSICBRAINZ_ALBUMID": album_a2,
                "ALBUMARTIST": "Artist A",
                "DISCNUMBER": "1",
            },
        )

        # --- Shape 2: per-medium-credit-variance ---
        # ArtistC/WorkC [rel 2002]/disc1/track1.flac — album_c, disc 1, albumartist "Artist C"
        _write_flac_stub(
            os.path.join(tmpdir, "ArtistC", "WorkC [rel 2002]", "disc1", "track1.flac"),
            {
                "MUSICBRAINZ_RELEASEGROUPID": rg_b,
                "MUSICBRAINZ_ALBUMID": album_c,
                "ALBUMARTIST": "Artist C",
                "DISCNUMBER": "1",
            },
        )
        # ArtistC/WorkC [rel 2002]/disc2/track1.flac — album_c, disc 2, albumartist "Artist C & D" (variance)
        _write_flac_stub(
            os.path.join(tmpdir, "ArtistC", "WorkC [rel 2002]", "disc2", "track1.flac"),
            {
                "MUSICBRAINZ_RELEASEGROUPID": rg_b,
                "MUSICBRAINZ_ALBUMID": album_c,
                "ALBUMARTIST": "Artist C & D",
                "DISCNUMBER": "2",
            },
        )

        # --- Shape 3: rg-vs-release-split ---
        # ArtistD/WorkD [rel 2003]/disc1/track1.flac — rg_c, album_d, albumartist "Artist D"
        # ArtistE_alias/WorkD [rel 2003]/disc1/track1.flac — rg_c, album_d, albumartist "D, Artist" (different top_dir)
        # One album MBID, two top_dirs, two ALBUMARTIST values → rg-vs-release-split (not rg-multi-release)
        _write_flac_stub(
            os.path.join(tmpdir, "ArtistD", "WorkD [rel 2003]", "disc1", "track1.flac"),
            {
                "MUSICBRAINZ_RELEASEGROUPID": rg_c,
                "MUSICBRAINZ_ALBUMID": album_d,
                "ALBUMARTIST": "Artist D",
                "DISCNUMBER": "1",
            },
        )
        _write_flac_stub(
            os.path.join(tmpdir, "ArtistD_alias", "WorkD [rel 2003]", "disc1", "track1.flac"),
            {
                "MUSICBRAINZ_RELEASEGROUPID": rg_c,
                "MUSICBRAINZ_ALBUMID": album_d,
                "ALBUMARTIST": "D, Artist",
                "DISCNUMBER": "1",
            },
        )

        # --- Clean release (no fragmentation) ---
        _write_flac_stub(
            os.path.join(tmpdir, "ArtistE", "WorkE [rel 2004]", "disc1", "track1.flac"),
            {
                "MUSICBRAINZ_RELEASEGROUPID": rg_d,
                "MUSICBRAINZ_ALBUMID": album_e,
                "ALBUMARTIST": "Artist E",
                "DISCNUMBER": "1",
            },
        )

        # --- Additional clean release to ensure rg_e is not confused with rg_d ---
        _write_flac_stub(
            os.path.join(tmpdir, "ArtistF", "WorkF [rel 2005]", "disc1", "track1.flac"),
            {
                "MUSICBRAINZ_RELEASEGROUPID": rg_e,
                "MUSICBRAINZ_ALBUMID": "al-ffff-0000-0000-0000-000000000006",
                "ALBUMARTIST": "Artist F",
                "DISCNUMBER": "1",
            },
        )

        # Run the scanner against the fixture root
        records = _collect_files(tmpdir)
        files_read = len(records)

        rg_multi = _detect_rg_multi_release(records)
        credit_variance = _detect_per_medium_credit_variance(records)
        rg_vs_release_raw = _detect_rg_vs_release_split(records)
        rg_vs_release = _deduplicate_rg_vs_release(rg_multi, rg_vs_release_raw)

        output = _build_output(rg_multi + credit_variance + rg_vs_release, files_read, "ok")
        summary = output["summary"]
        assert isinstance(summary, dict)
        findings = output["findings"]
        assert isinstance(findings, list)

        # --- Assertions ---

        # files_read: 7 stubs (ArtistA, ArtistB, ArtistC disc1, ArtistC disc2, ArtistD, ArtistD_alias, ArtistE, ArtistF)
        expected_files = 8
        if files_read != expected_files:
            failures.append(f"files_read: expected {expected_files}, got {files_read}")

        if summary["scan_status"] != "ok":
            failures.append(f"scan_status: expected 'ok', got {summary['scan_status']!r}")

        # rg-multi-release: exactly 1 (rg_a spans ArtistA + ArtistB with 2 album IDs)
        rg_multi_findings = [f for f in findings if f["shape"] == "rg-multi-release"]
        if len(rg_multi_findings) != 1:
            failures.append(f"rg-multi-release count: expected 1, got {len(rg_multi_findings)}")
        else:
            f0 = rg_multi_findings[0]
            if f0["release_group_id"] != rg_a:
                failures.append(f"rg-multi-release rg_id: expected {rg_a!r}, got {f0['release_group_id']!r}")
            f0_album_ids = f0["album_ids"]
            assert isinstance(f0_album_ids, list)
            if sorted(str(x) for x in f0_album_ids) != sorted([album_a1, album_a2]):
                failures.append(f"rg-multi-release album_ids: expected {sorted([album_a1, album_a2])}, got {f0_album_ids}")
            f0_top_dirs = f0["top_dirs"]
            assert isinstance(f0_top_dirs, list)
            if sorted(str(x) for x in f0_top_dirs) != ["ArtistA", "ArtistB"]:
                failures.append(f"rg-multi-release top_dirs: expected ['ArtistA', 'ArtistB'], got {f0_top_dirs}")
            if f0["remedy_route"] != "undetermined":
                failures.append(f"rg-multi-release remedy_route: expected 'undetermined', got {f0['remedy_route']!r}")

        # per-medium-credit-variance: exactly 1 (album_c has two discs with different ALBUMARTIST)
        credit_findings = [f for f in findings if f["shape"] == "per-medium-credit-variance"]
        if len(credit_findings) != 1:
            failures.append(f"per-medium-credit-variance count: expected 1, got {len(credit_findings)}")
        else:
            f1 = credit_findings[0]
            if f1["album_ids"] != [album_c]:
                failures.append(f"per-medium-credit-variance album_ids: expected [{album_c!r}], got {f1['album_ids']}")
            if f1["remedy_route"] != "undetermined":
                failures.append(f"per-medium-credit-variance remedy_route: expected 'undetermined', got {f1['remedy_route']!r}")

        # rg-vs-release-split: exactly 1 (rg_c spans ArtistD + ArtistD_alias with 1 album ID but 2 ALBUMARTIST values)
        split_findings = [f for f in findings if f["shape"] == "rg-vs-release-split"]
        if len(split_findings) != 1:
            failures.append(f"rg-vs-release-split count: expected 1, got {len(split_findings)}")
        else:
            f2 = split_findings[0]
            if f2["release_group_id"] != rg_c:
                failures.append(f"rg-vs-release-split rg_id: expected {rg_c!r}, got {f2['release_group_id']!r}")
            f2_top_dirs = f2["top_dirs"]
            assert isinstance(f2_top_dirs, list)
            if sorted(str(x) for x in f2_top_dirs) != ["ArtistD", "ArtistD_alias"]:
                failures.append(
                    f"rg-vs-release-split top_dirs: expected ['ArtistD', 'ArtistD_alias'], got {f2_top_dirs}"
                )
            if f2["remedy_route"] != "undetermined":
                failures.append(f"rg-vs-release-split remedy_route: expected 'undetermined', got {f2['remedy_route']!r}")

        # No findings for clean releases (rg_d / ArtistE, rg_e / ArtistF)
        clean_rg_ids = {rg_d, rg_e}
        for finding in findings:
            if finding["release_group_id"] in clean_rg_ids:
                failures.append(
                    f"False positive on clean release: shape={finding['shape']!r} rg_id={finding['release_group_id']!r}"
                )

        # Total finding count: exactly 3
        if len(findings) != 3:  # noqa: PLR2004 — 3 is the expected count (one per shape)
            failures.append(f"total findings: expected 3, got {len(findings)}")

    if failures:
        print("FIXTURE TEST FAILED:", file=sys.stderr)
        for msg in failures:
            print(f"  FAIL: {msg}", file=sys.stderr)
        return 1

    print("FIXTURE TEST PASSED: all assertions satisfied.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments and dispatch to the fixture test or the live scan.

    ``--test`` / ``--fixture``: run the KAT fixture test (exits 0 on pass, non-zero on failure).
    Default (no flag): run the live scan against ``ROOT``, print a human summary to stdout, and
    write JSON to ``docs/census-fragmentation.json`` (or ``--out`` if specified).
    """
    parser = argparse.ArgumentParser(
        description="Scan the annotated library for cross-medium fragmentation shapes.",
    )
    parser.add_argument(
        "--test",
        "--fixture",
        action="store_true",
        dest="test",
        help="Run the KAT fixture test instead of the live scan.",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Write JSON output to PATH (default: docs/census-fragmentation.json).",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        dest="json_only",
        help="Suppress the human-readable summary; write JSON only.",
    )
    args = parser.parse_args()

    if args.test:
        sys.exit(run_fixture_test())

    output = scan(ROOT)
    summary = output["summary"]
    assert isinstance(summary, dict)

    if not args.json_only:
        _print_human_summary(output)

    out_path = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs",
        "census-fragmentation.json",
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"JSON written to: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
