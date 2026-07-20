#!/usr/bin/env python3
"""Census tool for Original/ — Pass 1 offline evidence sweep.

Classifies every top-level directory in ``Original/`` into a two-axis taxonomy
(C-R0-TAX) using local evidence only (zero network calls):

- **Axis 1 — provenance**: bach-edition, presto, whipper, amazon, other-download, unknown
- **Axis 2 — MB status**: already-ingested, in-mb-clean, in-mb-mismatch, not-in-mb,
  non-classical-other, unknown

Pass 1 evidence sources:

1. Shape stats — file formats/extensions, track counts, disc-subdir structure, total size.
2. Embedded-tag probe (mutagen, first-file-per-disc sampling; ``--full-scan`` for all files):
   ``MUSICBRAINZ_ALBUMID`` (→ ``in-mb-clean``), ISRC presence (→ presto signal),
   vendor/comment tag signatures (→ amazon), genre tags (→ non-classical-other signal).
3. Sidecar artifacts — ``whipper.log``, ``.cue``, AccurateRip logs (→ whipper); booklet PDFs
   (→ presto); vendor manifests (→ amazon).
4. Collision probe — parse the journal; join each census dir against journal ``source`` fields
   on paths relative to ``Original/`` (absolute-path joins are the documented silent-no-op
   hazard; see NOTES.md "Note on host paths").  A dir whose files match journal entries *and*
   whose journal destinations exist under ``Done/`` is ``already-ingested`` (delete-candidate).

Journal action vocabulary (inspected 2026-07-20): ``tagged`` is the ingest action (not
``copied``).  The probe matches ``{"tagged"}`` as the candidate set.

Outputs two files (``--out`` prefix, default ``docs/census-r0``):

- ``<prefix>.json`` — one row per top-level dir, schema per C-R0-TAX contract.
- ``<prefix>.md`` — human summary: joint-distribution table, per-class dir listings,
  ambiguity queue for S2.

Read-only invariant: this script never writes, moves, or deletes anything under ``Original/``,
``Done/``, or ``Reference/``.

Ad-hoc analysis tool, not enrolled in tox gates (test/mypy/lint/format target src/ tests/ only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

JSON = dict[str, Any] | list[Any] | str | float | int | bool | None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUDIO_EXTS: frozenset[str] = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".wav", ".aiff", ".aif", ".wv", ".ape"})

# Sidecar artifact patterns (case-insensitive filename matching)
WHIPPER_PATTERNS: tuple[str, ...] = ("whipper.log",)
CUE_EXT: str = ".cue"
ACCURATERIP_PATTERNS: tuple[str, ...] = ("accuraterip", "accurate rip")
LOG_EXT: str = ".log"
# FreeDB disc info sidecar — present in old EAC/whipper rips
DISC_INFO_YAML: str = "disc info.yaml"
# cdda file naming pattern — old rip format
CDDA_PATTERN: str = ".cdda."

# Amazon tag signatures
AMAZON_TAG_KEYS: frozenset[str] = frozenset({"amazon.com song id", "amazon music"})
AMAZON_PRIV_OWNER: str = "www.amazon.com"
AMAZON_COMMENT_PATTERN: str = "amazon.com song id"

# Non-classical genre keywords (case-insensitive)
NON_CLASSICAL_GENRES: frozenset[str] = frozenset({
    "audiobook", "spoken word", "comedy", "dance", "electronic", "hip hop", "hip-hop",
    "pop", "rock", "r&b", "soul", "jazz", "country", "folk", "reggae", "latin",
    "children", "kids", "karaoke", "educational", "education", "new age", "ambient",
    "world", "gospel", "christian", "religious", "holiday", "christmas",
})

# Journal ingest action vocabulary (inspected 2026-07-20)
JOURNAL_INGEST_ACTIONS: frozenset[str] = frozenset({"tagged"})

# Bach Edition directory naming patterns
BACH_EDITION_PATTERNS: tuple[str, ...] = (
    "bach edition",
    "brilliant classics bach",
    "complete bach",
)

# Presto artifact patterns
PRESTO_PATTERNS: tuple[str, ...] = ("booklet",)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tag(tags: dict[str, list[str]], key: str) -> str:
    """Return the first value for a tag key, or empty string if absent.

    :param tags: Tag dict mapping lowercase key to list of string values.
    :param key: Tag key (lowercase).
    :return: First value string, or ``""`` if key absent or empty.
    """
    vals = tags.get(key.lower())
    return vals[0] if vals else ""


def _read_tags_flac(path: Path) -> dict[str, list[str]]:
    """Read FLAC tags via mutagen, returning a lowercase-keyed dict.

    :param path: Path to FLAC file.
    :return: Dict mapping lowercase tag key to list of string values.
    :raises Exception: On any mutagen read error (caller catches).
    """
    from mutagen.flac import FLAC  # type: ignore[import-untyped]

    audio = FLAC(str(path))
    if not audio.tags:
        return {}
    return {k.lower(): list(v) for k, v in audio.tags.items()}


def _read_tags_mp3(path: Path) -> dict[str, list[str]]:
    """Read MP3 ID3 tags via mutagen, returning a lowercase-keyed dict.

    Extracts text frames (TIT2, TPE1, TALB, TCON, TRCK, TPOS, ISRC, COMM, PRIV) into
    a normalised lowercase dict.  COMM frames are stored under ``comm::<lang>`` and
    ``comm::<desc>:<lang>``; PRIV frames under ``priv::<owner>``.

    :param path: Path to MP3 file.
    :return: Dict mapping lowercase tag key to list of string values.
    :raises Exception: On any mutagen read error (caller catches).
    """
    from mutagen.mp3 import MP3  # type: ignore[import-untyped]

    audio = MP3(str(path))
    if not audio.tags:
        return {}
    result: dict[str, list[str]] = {}
    for frame_id, frame in audio.tags.items():
        key = frame_id.lower()
        # Text frames have a .text attribute
        if hasattr(frame, "text"):
            result[key] = [str(t) for t in frame.text]
        elif hasattr(frame, "data"):
            # PRIV frames — store owner as sub-key
            owner = getattr(frame, "owner", "")
            result[f"priv::{owner.lower()}"] = [repr(frame.data[:32])]
        else:
            result[key] = [str(frame)]
    return result


def _read_tags(path: Path) -> dict[str, list[str]]:
    """Read audio tags from a FLAC or MP3 file.

    :param path: Path to audio file.
    :return: Lowercase-keyed tag dict, or empty dict on error.
    """
    try:
        suffix = path.suffix.lower()
        if suffix == ".flac":
            return _read_tags_flac(path)
        if suffix == ".mp3":
            return _read_tags_mp3(path)
    except Exception:  # noqa: BLE001
        pass
    return {}


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file.

    :param path: File path.
    :return: Hex digest string.
    :raises OSError: On read error.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Shape stats
# ---------------------------------------------------------------------------


def _collect_shape_stats(top_dir: Path) -> dict[str, Any]:
    """Collect shape statistics for a top-level directory.

    Walks the directory tree and counts audio files, formats, disc subdirs, and total size.

    :param top_dir: Top-level directory path.
    :return: Dict with keys: total_files, audio_files, formats (list), disc_subdirs (list),
             total_size_bytes, has_subdirs.
    """
    total_files = 0
    audio_files = 0
    total_size = 0
    formats: set[str] = set()
    disc_subdirs: list[str] = []
    has_subdirs = False

    for entry in os.scandir(top_dir):
        if entry.is_dir(follow_symlinks=False):
            has_subdirs = True
            disc_subdirs.append(entry.name)
            # Count files in subdir (one level deep for disc structure)
            for sub_entry in os.scandir(entry.path):
                if sub_entry.is_file(follow_symlinks=False):
                    total_files += 1
                    total_size += sub_entry.stat().st_size
                    ext = Path(sub_entry.name).suffix.lower()
                    if ext in AUDIO_EXTS:
                        audio_files += 1
                        formats.add(ext)
        elif entry.is_file(follow_symlinks=False):
            total_files += 1
            total_size += entry.stat().st_size
            ext = Path(entry.name).suffix.lower()
            if ext in AUDIO_EXTS:
                audio_files += 1
                formats.add(ext)

    # For nested structures (e.g. Amazon Music with sub-subdirs), do a full walk
    if has_subdirs and audio_files == 0:
        total_files = 0
        audio_files = 0
        total_size = 0
        formats = set()
        disc_subdirs = []
        for root, dirs, files in os.walk(top_dir):
            rel = Path(root).relative_to(top_dir)
            depth = len(rel.parts)
            if depth == 1:
                disc_subdirs.append(rel.parts[0])
            for fname in files:
                fpath = Path(root) / fname
                try:
                    total_files += 1
                    total_size += fpath.stat().st_size
                    ext = fpath.suffix.lower()
                    if ext in AUDIO_EXTS:
                        audio_files += 1
                        formats.add(ext)
                except OSError:
                    pass

    return {
        "total_files": total_files,
        "audio_files": audio_files,
        "formats": sorted(formats),
        "disc_subdirs": sorted(set(disc_subdirs)),
        "total_size_bytes": total_size,
        "has_subdirs": has_subdirs,
    }


# ---------------------------------------------------------------------------
# Sidecar artifact probe
# ---------------------------------------------------------------------------


def _probe_sidecars(top_dir: Path) -> dict[str, Any]:
    """Probe sidecar artifacts for provenance signals.

    Walks the directory tree looking for whipper logs, .cue files, AccurateRip logs,
    booklet PDFs, and Amazon manifest files.

    :param top_dir: Top-level directory path.
    :return: Dict with boolean keys: has_whipper_log, has_cue, has_accuraterip_log,
             has_booklet_pdf, has_log_files, sidecar_files (list of notable filenames).
    """
    has_whipper_log = False
    has_cue = False
    has_accuraterip_log = False
    has_booklet_pdf = False
    has_log_files = False
    has_disc_info_yaml = False
    has_cdda_files = False
    sidecar_files: list[str] = []

    for root, _dirs, files in os.walk(top_dir):
        for fname in files:
            fname_lower = fname.lower()
            if any(p in fname_lower for p in WHIPPER_PATTERNS):
                has_whipper_log = True
                sidecar_files.append(fname)
            if fname_lower.endswith(CUE_EXT):
                has_cue = True
                sidecar_files.append(fname)
            if fname_lower.endswith(LOG_EXT):
                has_log_files = True
                if any(p in fname_lower for p in ACCURATERIP_PATTERNS):
                    has_accuraterip_log = True
                    sidecar_files.append(fname)
            if fname_lower.endswith(".pdf"):
                has_booklet_pdf = True
                sidecar_files.append(fname)
            if DISC_INFO_YAML in fname_lower:
                has_disc_info_yaml = True
                sidecar_files.append(fname)
            if CDDA_PATTERN in fname_lower:
                has_cdda_files = True

    return {
        "has_whipper_log": has_whipper_log,
        "has_cue": has_cue,
        "has_accuraterip_log": has_accuraterip_log,
        "has_booklet_pdf": has_booklet_pdf,
        "has_log_files": has_log_files,
        "has_disc_info_yaml": has_disc_info_yaml,
        "has_cdda_files": has_cdda_files,
        "sidecar_files": sidecar_files[:20],  # cap for JSON size
    }


# ---------------------------------------------------------------------------
# Tag probe
# ---------------------------------------------------------------------------


def _find_sample_audio_files(top_dir: Path, full_scan: bool) -> list[Path]:
    """Find audio files to probe for tags.

    In default (sampled) mode, returns the first audio file per disc subdir (or the first
    audio file in the top dir if flat).  In ``--full-scan`` mode, returns all audio files.

    :param top_dir: Top-level directory path.
    :param full_scan: If True, return all audio files.
    :return: List of audio file paths to probe.
    """
    audio_files: list[Path] = []

    # Check if top dir has direct audio files (flat layout)
    direct_audio: list[Path] = []
    subdirs: list[Path] = []
    for entry in os.scandir(top_dir):
        p = Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            subdirs.append(p)
        elif entry.is_file(follow_symlinks=False) and p.suffix.lower() in AUDIO_EXTS:
            direct_audio.append(p)

    if direct_audio:
        if full_scan:
            audio_files.extend(sorted(direct_audio))
        else:
            audio_files.append(sorted(direct_audio)[0])

    # For each subdir (disc), sample the first audio file
    for subdir in sorted(subdirs):
        subdir_audio: list[Path] = []
        for root, _dirs, files in os.walk(subdir):
            for fname in sorted(files):
                p = Path(root) / fname
                if p.suffix.lower() in AUDIO_EXTS:
                    subdir_audio.append(p)
            if not full_scan and subdir_audio:
                break  # first file found in this subdir is enough
        if full_scan:
            audio_files.extend(subdir_audio)
        elif subdir_audio:
            audio_files.append(subdir_audio[0])

    return audio_files


def _probe_tags(top_dir: Path, full_scan: bool) -> dict[str, Any]:
    """Probe embedded tags for classification signals.

    Reads tags from sampled (or all) audio files and extracts:
    - MUSICBRAINZ_ALBUMID presence and values
    - ISRC presence
    - Amazon tag signatures
    - Genre tags (for non-classical detection)

    :param top_dir: Top-level directory path.
    :param full_scan: If True, probe all audio files; otherwise sample first per disc.
    :return: Dict with tag evidence fields.
    """
    sample_files = _find_sample_audio_files(top_dir, full_scan)

    mbids: list[str] = []
    has_isrc = False
    has_amazon_tags = False
    genres: list[str] = []
    files_probed = 0
    files_with_tags = 0

    for fpath in sample_files:
        tags = _read_tags(fpath)
        if not tags:
            continue
        files_probed += 1
        files_with_tags += 1

        # MUSICBRAINZ_ALBUMID
        mbid = _get_tag(tags, "musicbrainz_albumid")
        if mbid and mbid not in mbids:
            mbids.append(mbid)

        # ISRC
        if _get_tag(tags, "isrc"):
            has_isrc = True

        # Amazon tag signatures
        # Check COMM frames for Amazon.com Song ID
        for key, vals in tags.items():
            if "comm" in key:
                for v in vals:
                    if AMAZON_COMMENT_PATTERN in v.lower():
                        has_amazon_tags = True
            if "priv" in key and AMAZON_PRIV_OWNER in key:
                has_amazon_tags = True

        # Genre tags
        genre = _get_tag(tags, "genre")
        if not genre:
            # ID3 TCON
            genre = _get_tag(tags, "tcon")
        if genre and genre not in genres:
            genres.append(genre)

    return {
        "mbids": mbids,
        "has_isrc": has_isrc,
        "has_amazon_tags": has_amazon_tags,
        "genres": genres,
        "files_probed": files_probed,
        "files_with_tags": files_with_tags,
        "sample_file_count": len(sample_files),
    }


# ---------------------------------------------------------------------------
# Collision probe (journal detects)
# ---------------------------------------------------------------------------


def _build_journal_index(journal_path: Path, original_root: Path) -> dict[str, list[dict[str, str]]]:
    """Parse the journal and build an index from top-level dir name to journal entries.

    Joins on paths relative to ``Original/`` — absolute-path joins are the documented
    silent-no-op hazard (NOTES.md "Note on host paths").

    :param journal_path: Path to the journal JSON file.
    :param original_root: Canonical ``Original/`` root (e.g. ``/home/findlay/Music/Original``).
    :return: Dict mapping top-level dir name (relative to Original/) to list of journal entries
             with keys: source, destination, action, timestamp.
    """
    try:
        with journal_path.open() as fh:
            entries: list[dict[str, Any]] = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: cannot read journal {journal_path}: {exc}", file=sys.stderr)
        return {}

    index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for entry in entries:
        action = entry.get("action", "")
        if action not in JOURNAL_INGEST_ACTIONS:
            continue
        source = entry.get("source", "")
        if not source:
            continue
        src_path = Path(source)
        try:
            rel = src_path.relative_to(original_root)
        except ValueError:
            continue
        top_dir_name = rel.parts[0]
        index[top_dir_name].append({
            "source": source,
            "destination": entry.get("destination", ""),
            "action": action,
            "timestamp": entry.get("timestamp", ""),
        })

    return dict(index)


def _probe_collision(
    dir_name: str,
    journal_index: dict[str, list[dict[str, str]]],
    done_local: Path,
    done_canonical: Path,
    verify_hashes: bool,
    top_dir: Path,
    original_root: Path,
) -> dict[str, Any]:
    """Probe whether a directory has already been ingested (collision class).

    Checks journal entries for this dir and verifies that destination files exist under Done/.
    Journal destinations use canonical paths (``/home/findlay/Music/Done/...``); this function
    maps them to local mount paths for existence checks.

    :param dir_name: Top-level directory name (relative to Original/).
    :param journal_index: Pre-built journal index from :func:`_build_journal_index`.
    :param done_local: Local mount path to Done/ (e.g. ``~/Remote/hades/Music/Done``).
    :param done_canonical: Canonical Done/ root as recorded in journal destinations
           (e.g. ``/home/findlay/Music/Done``).
    :param verify_hashes: If True, re-verify SHA-256 of source vs destination.
    :param top_dir: Absolute path to the top-level directory on the local mount.
    :param original_root: Canonical ``Original/`` root (for relative-path joins).
    :return: Dict with collision evidence fields.
    """
    entries = journal_index.get(dir_name, [])
    journal_entry_count = len(entries)

    if journal_entry_count == 0:
        return {
            "journal_entry_count": 0,
            "destination_present_count": 0,
            "source_file_count": 0,
            "hash_verified_count": 0,
            "is_collision_candidate": False,
        }

    # Count source files in the dir
    source_file_count = 0
    for root, _dirs, files in os.walk(top_dir):
        for fname in files:
            if Path(fname).suffix.lower() in AUDIO_EXTS:
                source_file_count += 1

    # Check how many destinations exist under Done/
    # Journal destinations use canonical paths; map to local mount via relative-path join.
    destination_present_count = 0
    hash_verified_count = 0

    for entry in entries:
        dest_path_str = entry.get("destination", "")
        if not dest_path_str:
            continue
        dest_canonical_path = Path(dest_path_str)
        # Relativize against canonical Done/ root, then map to local mount
        try:
            dest_rel = dest_canonical_path.relative_to(done_canonical)
        except ValueError:
            continue
        local_dest = done_local / dest_rel
        if local_dest.exists():
            destination_present_count += 1
            if verify_hashes:
                src_rel_str = entry.get("source", "")
                if src_rel_str:
                    src_canonical = Path(src_rel_str)
                    try:
                        src_rel = src_canonical.relative_to(original_root)
                    except ValueError:
                        continue
                    local_src = top_dir.parent / src_rel
                    if local_src.exists():
                        try:
                            src_hash = _sha256_file(local_src)
                            dest_hash = _sha256_file(local_dest)
                            if src_hash == dest_hash:
                                hash_verified_count += 1
                        except OSError:
                            pass

    is_collision_candidate = (
        journal_entry_count > 0
        and destination_present_count > 0
        and destination_present_count >= journal_entry_count * 0.5  # at least half present
    )

    return {
        "journal_entry_count": journal_entry_count,
        "destination_present_count": destination_present_count,
        "source_file_count": source_file_count,
        "hash_verified_count": hash_verified_count,
        "is_collision_candidate": is_collision_candidate,
    }


# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------


def _classify_provenance(
    dir_name: str,
    shape: dict[str, Any],
    sidecars: dict[str, Any],
    tags: dict[str, Any],
) -> tuple[str, list[str]]:
    """Classify Axis 1 provenance from local evidence.

    Priority order: bach-edition > whipper > presto > amazon > other-download > unknown.

    :param dir_name: Top-level directory name.
    :param shape: Shape stats from :func:`_collect_shape_stats`.
    :param sidecars: Sidecar probe from :func:`_probe_sidecars`.
    :param tags: Tag probe from :func:`_probe_tags`.
    :return: Tuple of (provenance class string, list of evidence strings).
    """
    evidence: list[str] = []
    dir_lower = dir_name.lower()

    # Bach Edition: Brilliant Classics Bach Edition remainder
    if any(p in dir_lower for p in BACH_EDITION_PATTERNS):
        evidence.append(f"dir name matches bach-edition pattern: {dir_name!r}")
        return "bach-edition", evidence

    # Whipper: whipper.log / .cue / AccurateRip artifacts / rip-log sidecars /
    # disc info.yaml (FreeDB sidecar) / .cdda. file naming / .0x hex suffix in dir name
    import re as _re
    if sidecars["has_whipper_log"]:
        evidence.append("whipper.log present")
        return "whipper", evidence
    if sidecars["has_accuraterip_log"]:
        evidence.append("AccurateRip log present")
        return "whipper", evidence
    if sidecars["has_disc_info_yaml"]:
        evidence.append("disc info.yaml (FreeDB sidecar) present")
        return "whipper", evidence
    if sidecars["has_cdda_files"]:
        evidence.append(".cdda. file naming (old rip format)")
        return "whipper", evidence
    if _re.search(r"\.0x[0-9a-f]{6,}$", dir_name, _re.IGNORECASE):
        evidence.append(f"dir name has .0x hex checksum suffix (old rip format): {dir_name!r}")
        return "whipper", evidence
    if sidecars["has_cue"] and sidecars["has_log_files"]:
        evidence.append(".cue + .log files present (rip artifacts)")
        return "whipper", evidence

    # Presto: booklet PDFs (strong signal) + optionally ISRC-bearing tags
    # ISRC alone is too broad (present in many download formats); PDF is Presto-specific
    if sidecars["has_booklet_pdf"]:
        evidence.append("booklet PDF present")
        if tags["has_isrc"]:
            evidence.append("ISRC tags present")
        return "presto", evidence

    # Amazon: vendor tag signatures, Amazon manifest files
    if tags["has_amazon_tags"]:
        evidence.append("Amazon tag signatures present (COMM/PRIV frames)")
        return "amazon", evidence
    if dir_lower == "amazon music" or dir_lower.startswith("amazon music/"):
        evidence.append("directory named 'Amazon Music'")
        return "amazon", evidence

    # Other-download: downloaded provenance evident but vendor not identified
    # Signals: .cue without log (download-style), numeric catalog IDs in dir name, ISRC-only
    if sidecars["has_cue"]:
        evidence.append(".cue file present (download artifact)")
        return "other-download", evidence

    # ISRC tags present but no booklet PDF — generic download, vendor unidentified
    if tags["has_isrc"]:
        evidence.append("ISRC tags present (download, vendor unidentified)")
        return "other-download", evidence

    # Numeric catalog ID patterns (e.g. "4757765 - Berlioz Requiem", "CHAN9177 - ...")
    import re
    if re.match(r"^\d{5,}", dir_name) or re.match(r"^[A-Z]{2,}\d{3,}", dir_name):
        evidence.append(f"catalog ID prefix in dir name: {dir_name!r}")
        return "other-download", evidence

    # No provenance signal
    return "unknown", evidence


def _classify_mb_status(
    dir_name: str,
    tags: dict[str, Any],
    collision: dict[str, Any],
    shape: dict[str, Any],
) -> tuple[str, list[str]]:
    """Classify Axis 2 MB status from local evidence.

    :param dir_name: Top-level directory name.
    :param tags: Tag probe from :func:`_probe_tags`.
    :param collision: Collision probe from :func:`_probe_collision`.
    :param shape: Shape stats from :func:`_collect_shape_stats`.
    :return: Tuple of (mb_status class string, list of evidence strings).
    """
    evidence: list[str] = []
    dir_lower = dir_name.lower()

    # Already-ingested: journal source match + destination present
    if collision["is_collision_candidate"]:
        evidence.append(
            f"journal: {collision['journal_entry_count']} entries, "
            f"{collision['destination_present_count']} destinations present"
        )
        return "already-ingested", evidence

    # Non-classical-other: audiobooks, dance, education, etc.
    # Use conservative keyword matching to avoid false positives on classical music
    # (e.g. "Dances & Marches", "ballet suite", "invitation to the dance" are classical).
    non_classical_signals = 0
    non_classical_reasons: list[str] = []

    # Directory name signals — only unambiguous non-classical keywords
    non_classical_dir_keywords = (
        "audiobook", "audiobooks", "karaoke", "hypno",
        "yoga", "meditation", "lullaby", "nursery", "spoken word", "comedy", "podcast",
        "garageband", "exercise", "fitness", "workout",
    )
    for kw in non_classical_dir_keywords:
        if kw in dir_lower:
            non_classical_signals += 1
            non_classical_reasons.append(f"dir name contains {kw!r}")
            break

    # "kids" / "kidz" as standalone words (not part of "kidz bop" in a classical context)
    import re as _re2
    if _re2.search(r"\bkidz?\b", dir_lower):
        non_classical_signals += 1
        non_classical_reasons.append("dir name contains 'kids/kidz'")

    # Genre tag signals — only when genre is clearly non-classical
    # Avoid false positives: 'Electronic' can appear on Shostakovich downloads; 'Dance' on
    # classical ballet; 'Children' on educational classical.  Require multiple genre signals
    # or a clearly non-classical genre (pop, rock, hip-hop, r&b, karaoke, audiobook).
    clearly_non_classical_genres = frozenset({
        "pop", "rock", "hip hop", "hip-hop", "r&b", "rap", "karaoke",
        "audiobook", "spoken word", "comedy", "dance & dj", "rap & hip-hop",
        "christian & gospel", "children's music",
    })
    genre_signals = 0
    for genre in tags.get("genres", []):
        genre_lower = genre.lower()
        for nc_genre in clearly_non_classical_genres:
            if nc_genre in genre_lower:
                genre_signals += 1
                non_classical_reasons.append(f"genre tag: {genre!r}")
                break

    # Require 2+ genre signals OR 1 unambiguous dir keyword + 1 genre signal
    if genre_signals >= 2:
        non_classical_signals += genre_signals
    elif genre_signals >= 1 and non_classical_signals >= 1:
        non_classical_signals += genre_signals

    if non_classical_signals >= 1:
        evidence.extend(non_classical_reasons)
        return "non-classical-other", evidence

    # In-MB-clean: embedded MUSICBRAINZ_ALBUMID
    if tags["mbids"]:
        evidence.append(f"MUSICBRAINZ_ALBUMID present: {tags['mbids'][:3]}")
        return "in-mb-clean", evidence

    # Unknown: Pass 1 could not determine; Pass 2 needed
    evidence.append("no MB status signal found in Pass 1")
    return "unknown", evidence


# ---------------------------------------------------------------------------
# Per-directory census
# ---------------------------------------------------------------------------


def _census_dir(
    top_dir: Path,
    dir_name: str,
    journal_index: dict[str, list[dict[str, str]]],
    done_local: Path,
    done_canonical: Path,
    original_root: Path,
    full_scan: bool,
    verify_hashes: bool,
) -> dict[str, Any]:
    """Collect all census evidence for one top-level directory.

    :param top_dir: Absolute path to the directory.
    :param dir_name: Directory name (relative to Original/).
    :param journal_index: Pre-built journal index.
    :param done_local: Local mount path to Done/ (for file existence checks).
    :param done_canonical: Canonical Done/ root as recorded in journal destinations.
    :param original_root: Canonical Original/ root (for relative-path joins).
    :param full_scan: If True, probe all audio files for tags.
    :param verify_hashes: If True, re-verify SHA-256 hashes in collision probe.
    :return: Census row dict per C-R0-TAX schema.
    """
    shape = _collect_shape_stats(top_dir)
    sidecars = _probe_sidecars(top_dir)
    tags = _probe_tags(top_dir, full_scan)
    collision = _probe_collision(
        dir_name, journal_index, done_local, done_canonical, verify_hashes, top_dir, original_root
    )

    axis1, prov_evidence = _classify_provenance(dir_name, shape, sidecars, tags)
    axis2, mb_evidence = _classify_mb_status(dir_name, tags, collision, shape)

    return {
        "dir": dir_name,
        "axis1": axis1,
        "axis2": axis2,
        "evidence": {
            "provenance": prov_evidence,
            "mb_status": mb_evidence,
        },
        "notes": "",
        "shape": shape,
        "tag_probe": {
            "mbids": tags["mbids"],
            "has_isrc": tags["has_isrc"],
            "has_amazon_tags": tags["has_amazon_tags"],
            "genres": tags["genres"],
            "files_probed": tags["files_probed"],
            "sample_file_count": tags["sample_file_count"],
        },
        "sidecar_probe": {
            "has_whipper_log": sidecars["has_whipper_log"],
            "has_cue": sidecars["has_cue"],
            "has_accuraterip_log": sidecars["has_accuraterip_log"],
            "has_booklet_pdf": sidecars["has_booklet_pdf"],
            "has_log_files": sidecars["has_log_files"],
            "has_disc_info_yaml": sidecars["has_disc_info_yaml"],
            "has_cdda_files": sidecars["has_cdda_files"],
            "sidecar_files": sidecars["sidecar_files"],
        },
        "collision_probe": collision,
    }


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------


def _generate_markdown(rows: list[dict[str, Any]], out_json_path: Path) -> str:
    """Generate the census-r0.md human summary.

    Produces: joint-distribution table (axis1 × axis2 counts), per-class dir listings,
    and ambiguity queue (dirs with unknown on either axis).

    :param rows: List of census row dicts.
    :param out_json_path: Path to the JSON artifact (for cross-reference).
    :return: Markdown string.
    """
    from collections import Counter

    # Build joint distribution
    joint: Counter[tuple[str, str]] = Counter()
    axis1_vals: list[str] = ["bach-edition", "presto", "whipper", "amazon", "other-download", "unknown"]
    axis2_vals: list[str] = [
        "already-ingested", "in-mb-clean", "in-mb-mismatch", "not-in-mb", "non-classical-other", "unknown"
    ]

    for row in rows:
        joint[(row["axis1"], row["axis2"])] += 1

    axis1_totals: Counter[str] = Counter()
    axis2_totals: Counter[str] = Counter()
    for (a1, a2), count in joint.items():
        axis1_totals[a1] += count
        axis2_totals[a2] += count

    lines: list[str] = []
    lines.append("# Census R0 — Pass 1 Offline Evidence Sweep")
    lines.append("")
    lines.append(f"Generated from `{out_json_path.name}` — {len(rows)} top-level dirs in `Original/`.")
    lines.append("")
    lines.append("## Joint Distribution (Axis 1 × Axis 2)")
    lines.append("")

    # Table header
    col_width = 20
    header = "| Provenance \\ MB Status |"
    for a2 in axis2_vals:
        header += f" {a2[:col_width]:<{col_width}} |"
    header += " **Total** |"
    lines.append(header)

    sep = "| --- |"
    for _ in axis2_vals:
        sep += " --- |"
    sep += " --- |"
    lines.append(sep)

    for a1 in axis1_vals:
        if axis1_totals[a1] == 0:
            continue
        row_str = f"| **{a1}** |"
        for a2 in axis2_vals:
            count = joint.get((a1, a2), 0)
            row_str += f" {count:<{col_width}} |"
        row_str += f" **{axis1_totals[a1]}** |"
        lines.append(row_str)

    # Totals row
    total_row = "| **Total** |"
    for a2 in axis2_vals:
        total_row += f" **{axis2_totals[a2]}** |"
    total_row += f" **{len(rows)}** |"
    lines.append(total_row)
    lines.append("")

    # Per-class listings
    lines.append("## Per-Class Directory Listings")
    lines.append("")

    # Group by (axis1, axis2)
    by_class: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        by_class[(row["axis1"], row["axis2"])].append(row["dir"])

    for a1 in axis1_vals:
        for a2 in axis2_vals:
            dirs = by_class.get((a1, a2), [])
            if not dirs:
                continue
            lines.append(f"### {a1} / {a2} ({len(dirs)} dirs)")
            lines.append("")
            for d in sorted(dirs):
                lines.append(f"- `{d}`")
            lines.append("")

    # Already-ingested delete-candidates with evidence
    already_ingested = [r for r in rows if r["axis2"] == "already-ingested"]
    if already_ingested:
        lines.append("## Already-Ingested Delete-Candidates (Evidence Detail)")
        lines.append("")
        lines.append("These dirs have journal matches with destinations present under `Done/`.")
        lines.append("Evidence level: journal-entry count / destination-present count / source-file count.")
        lines.append("")
        for row in sorted(already_ingested, key=lambda r: r["dir"]):
            cp = row["collision_probe"]
            lines.append(
                f"- `{row['dir']}` — journal: {cp['journal_entry_count']}, "
                f"dest present: {cp['destination_present_count']}, "
                f"source files: {cp['source_file_count']}"
            )
        lines.append("")

    # Ambiguity queue for S2
    ambiguous = [r for r in rows if r["axis1"] == "unknown" or r["axis2"] == "unknown"]
    lines.append("## Ambiguity Queue for S2 (Pass 2 Network Lookups)")
    lines.append("")
    if ambiguous:
        lines.append(
            f"{len(ambiguous)} dirs have `unknown` on at least one axis and require Pass 2 "
            "MB network lookups to resolve."
        )
        lines.append("")
        lines.append("| Dir | Axis 1 | Axis 2 | Notes |")
        lines.append("| --- | --- | --- | --- |")
        for row in sorted(ambiguous, key=lambda r: r["dir"]):
            notes = row.get("notes", "") or ""
            lines.append(f"| `{row['dir']}` | {row['axis1']} | {row['axis2']} | {notes} |")
        lines.append("")
    else:
        lines.append("No dirs with `unknown` on either axis — ambiguity queue is empty.")
        lines.append("")

    # Summary statistics
    lines.append("## Summary Statistics")
    lines.append("")
    lines.append(f"- Total dirs: {len(rows)}")
    lines.append(f"- Axis 1 distribution: {dict(sorted(axis1_totals.items()))}")
    lines.append(f"- Axis 2 distribution: {dict(sorted(axis2_totals.items()))}")
    lines.append(f"- Already-ingested (delete-candidates): {axis2_totals.get('already-ingested', 0)}")
    lines.append(f"- Ambiguous (unknown on either axis): {len(ambiguous)}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    :return: Parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description="Census tool for Original/ — Pass 1 offline evidence sweep.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--original",
        type=Path,
        default=Path.home() / "Remote/hades/Music/Original",
        help="Path to Original/ directory (default: ~/Remote/hades/Music/Original)",
    )
    parser.add_argument(
        "--done",
        type=Path,
        default=Path.home() / "Remote/hades/Music/Done",
        help="Path to Done/ directory (default: ~/Remote/hades/Music/Done)",
    )
    parser.add_argument(
        "--journal",
        type=Path,
        default=Path.home() / "Remote/hades/Music/Done/music_annotator_journal.json",
        help="Path to journal JSON file",
    )
    parser.add_argument(
        "--original-canonical",
        type=Path,
        default=Path("/home/findlay/Music/Original"),
        help="Canonical Original/ root as recorded in journal source paths (default: /home/findlay/Music/Original)",
    )
    parser.add_argument(
        "--done-canonical",
        type=Path,
        default=Path("/home/findlay/Music/Done"),
        help="Canonical Done/ root as recorded in journal destination paths (default: /home/findlay/Music/Done)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/census-r0"),
        help="Output file prefix (default: docs/census-r0); produces <prefix>.json and <prefix>.md",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Probe all audio files for tags (default: sample first file per disc dir)",
    )
    parser.add_argument(
        "--verify-hashes",
        action="store_true",
        help="Re-verify SHA-256 hashes for collision candidates (expensive over sshfs; default: off)",
    )
    return parser.parse_args()


def main() -> None:
    """Run the Pass 1 census sweep.

    Iterates all top-level dirs in Original/, collects local evidence, classifies each
    into the two-axis taxonomy, and writes census-r0.json and census-r0.md.
    """
    args = _parse_args()

    original_local: Path = args.original
    done_local: Path = args.done
    journal_path: Path = args.journal
    original_canonical: Path = args.original_canonical
    done_canonical: Path = args.done_canonical
    out_prefix: Path = args.out
    full_scan: bool = args.full_scan
    verify_hashes: bool = args.verify_hashes

    if not original_local.is_dir():
        print(f"ERROR: Original/ not found: {original_local}", file=sys.stderr)
        sys.exit(1)

    print(f"Census Pass 1: {original_local}", file=sys.stderr)
    print(f"  Done/ (local): {done_local}", file=sys.stderr)
    print(f"  Done/ (canonical): {done_canonical}", file=sys.stderr)
    print(f"  Journal: {journal_path}", file=sys.stderr)
    print(f"  Canonical Original/: {original_canonical}", file=sys.stderr)
    print(f"  Full scan: {full_scan}", file=sys.stderr)
    print(f"  Verify hashes: {verify_hashes}", file=sys.stderr)

    # Build journal index (relative-path joins on original_canonical)
    print("Building journal index...", file=sys.stderr)
    journal_index = _build_journal_index(journal_path, original_canonical)
    print(f"  Journal index: {len(journal_index)} top-level dirs with tagged entries", file=sys.stderr)

    # Enumerate top-level dirs
    top_dirs = sorted(
        entry.name
        for entry in os.scandir(original_local)
        if entry.is_dir(follow_symlinks=False)
    )
    print(f"  Top-level dirs: {len(top_dirs)}", file=sys.stderr)

    # Census each dir
    rows: list[dict[str, Any]] = []
    for i, dir_name in enumerate(top_dirs, 1):
        top_dir = original_local / dir_name
        print(f"  [{i:3d}/{len(top_dirs)}] {dir_name}", file=sys.stderr)
        row = _census_dir(
            top_dir=top_dir,
            dir_name=dir_name,
            journal_index=journal_index,
            done_local=done_local,
            done_canonical=done_canonical,
            original_root=original_canonical,
            full_scan=full_scan,
            verify_hashes=verify_hashes,
        )
        rows.append(row)

    # Write JSON
    out_json = out_prefix.with_suffix(".json")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
    print(f"Wrote {out_json} ({len(rows)} rows)", file=sys.stderr)

    # Write Markdown
    out_md = out_prefix.with_suffix(".md")
    md_content = _generate_markdown(rows, out_json)
    with out_md.open("w", encoding="utf-8") as fh:
        fh.write(md_content)
    print(f"Wrote {out_md}", file=sys.stderr)

    # Summary
    from collections import Counter
    axis1_dist = Counter(r["axis1"] for r in rows)
    axis2_dist = Counter(r["axis2"] for r in rows)
    unknown_count = sum(1 for r in rows if r["axis1"] == "unknown" or r["axis2"] == "unknown")
    print(f"\nAxis 1: {dict(sorted(axis1_dist.items()))}", file=sys.stderr)
    print(f"Axis 2: {dict(sorted(axis2_dist.items()))}", file=sys.stderr)
    print(f"Ambiguous (unknown on either axis): {unknown_count}", file=sys.stderr)


if __name__ == "__main__":
    main()
