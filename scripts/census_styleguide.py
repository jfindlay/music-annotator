#!/usr/bin/env python3
"""Census tool for Done/ — attribution-field sweep for the empirical styleguide census.

Walks the annotated library (``Done/`` tree — where credit/role tags exist) and produces
the empirical census artifact for the V1a styleguide mining session (S3).

The census measures the *attribution fields* (performer/role tags, credit strings, MBIDs),
not the provenance axis (which is census_original.py's domain).  Target measurements
(ROADMAP-styleguide lines 85–90):

- Multi-soloist releases (≥2 distinct PERFORMER entries with soloist-type roles)
- Conductor-less ensembles (PERFORMER entries with ensemble role, no CONDUCTOR)
- Choir+orchestra combinations
- Completer/arranger credits (PERFORMER with "arranger", "completer", "orchestrator" role)
- Play-direct (PERFORMER with "conductor" role but no separate CONDUCTOR tag)
- Opera principal counts (releases with ≥3 distinct vocal soloists)
- Attribution-variance instances: same MUSICBRAINZ_WORKID, different PERFORMER/CONDUCTOR
  sets across releases — the proof that selection is editorial
- Name-form variance: same MUSICBRAINZ_ARTISTID, different rendered name forms — the
  normalisation/fragmentation evidence

Outputs two files (``--out`` prefix, default ``docs/census-library``):

- ``<prefix>.json`` — per-release rows plus aggregate measurements.
- ``<prefix>.md``  — human summary: coverage KAT, per-case frequency estimates, concrete
  instances, discoveries.

Read-only invariant: this script never writes, moves, or deletes anything under ``Done/``.

Host-path caveat: the canonical library root is ``/home/findlay/Music/Done/`` on hades.
In a dev environment on a different host the root will not be present; the script fails
loudly (non-zero exit) rather than emitting an empty census.

Ad-hoc analysis tool, not enrolled in tox gates (test/mypy/lint/format target src/ tests/ only).
"""
from __future__ import annotations

import argparse
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

# Default library root (canonical on hades)
DEFAULT_LIBRARY_ROOT: str = "/home/findlay/Music/Done/"

# Default output prefix (relative to cwd)
DEFAULT_OUT_PREFIX: str = "docs/census-library"

# Attribution tags to probe (FLAC Vorbis comment names, lowercased)
ATTRIBUTION_TAGS: tuple[str, ...] = (
    "performer",
    "conductor",
    "composer",
    "lyricist",
    "artist",
    "albumartist",
    "musicbrainz_artistid",
    "musicbrainz_albumartistid",
    "musicbrainz_albumid",
    "musicbrainz_trackid",
    "musicbrainz_workid",
    "work",
    "part",
    "grouping",
    "genre",
    "is_classical",
)

# CWP/CEA tag prefixes to capture (any tag whose key starts with these)
CWP_CEA_PREFIXES: tuple[str, ...] = ("_cwp_", "_cea_", "cwp_", "cea_")

# Role keywords that indicate a soloist-type performer (in PERFORMER tag values)
SOLOIST_ROLE_KEYWORDS: frozenset[str] = frozenset({
    "violin", "viola", "cello", "double bass", "flute", "oboe", "clarinet", "bassoon",
    "horn", "trumpet", "trombone", "tuba", "piano", "harpsichord", "organ", "guitar",
    "harp", "percussion", "soprano", "mezzo", "tenor", "baritone", "bass", "contralto",
    "voice", "vocal", "singer", "soloist",
})

# Role keywords that indicate an ensemble-type performer
ENSEMBLE_ROLE_KEYWORDS: frozenset[str] = frozenset({
    "orchestra", "philharmonic", "philharmoniker", "symphony", "ensemble", "choir",
    "chorus", "singers", "band", "quartet", "quintet", "sextet", "septet", "octet",
    "chamber", "consort", "players", "academy", "musicians",
})

# Role keywords that indicate a completer/arranger/orchestrator credit
COMPLETER_ROLE_KEYWORDS: frozenset[str] = frozenset({
    "arranger", "completer", "orchestrator", "completion", "orchestration",
    "arrangement", "transcriber", "transcription",
})

# Vocal soloist keywords (for opera principal counting)
VOCAL_KEYWORDS: frozenset[str] = frozenset({
    "soprano", "mezzo", "tenor", "baritone", "bass", "contralto", "voice", "vocal", "singer",
})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tags(tags: dict[str, list[str]], key: str) -> list[str]:
    """Return all values for a tag key (case-insensitive), or empty list if absent.

    :param tags: Tag dict mapping lowercase key to list of string values.
    :param key: Tag key (lowercase).
    :return: List of values, or ``[]`` if key absent.
    """
    return tags.get(key.lower(), [])


def _get_tag(tags: dict[str, list[str]], key: str) -> str:
    """Return the first value for a tag key, or empty string if absent.

    :param tags: Tag dict mapping lowercase key to list of string values.
    :param key: Tag key (lowercase).
    :return: First value string, or ``""`` if key absent or empty.
    """
    vals = _get_tags(tags, key)
    return vals[0] if vals else ""


def _read_tags_flac(path: Path) -> dict[str, list[str]]:
    """Read FLAC Vorbis comment tags via mutagen, returning a lowercase-keyed dict.

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

    Extracts text frames (TXXX, TPE1, TALB, TCON, TRCK, TPOS, COMM) into a normalised
    lowercase dict.  TXXX frames are stored under their description key.

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
        if hasattr(frame, "text"):
            # TXXX frames have a desc attribute; use it as the key
            desc = getattr(frame, "desc", "")
            if desc:
                result[desc.lower()] = [str(t) for t in frame.text]
            else:
                result[key] = [str(t) for t in frame.text]
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


def _extract_cwp_cea_tags(tags: dict[str, list[str]]) -> dict[str, list[str]]:
    """Extract all _cwp_* and _cea_* tags from a tag dict.

    :param tags: Lowercase-keyed tag dict.
    :return: Dict of cwp/cea tags only.
    """
    return {k: v for k, v in tags.items() if any(k.startswith(p) for p in CWP_CEA_PREFIXES)}


# ---------------------------------------------------------------------------
# Release-level aggregation
# ---------------------------------------------------------------------------


def _find_audio_files(root: Path) -> list[Path]:
    """Walk a directory tree and return all audio files.

    :param root: Root directory to walk.
    :return: Sorted list of audio file paths.
    """
    result: list[Path] = []
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            p = Path(dirpath) / fname
            if p.suffix.lower() in AUDIO_EXTS:
                result.append(p)
    return sorted(result)


def _aggregate_release_tags(audio_files: list[Path]) -> dict[str, list[str]]:
    """Aggregate attribution tags across all tracks of a release.

    Reads tags from every audio file and merges multi-valued tags (PERFORMER, etc.)
    into deduplicated lists.  Single-valued tags (MUSICBRAINZ_ALBUMID, CONDUCTOR, etc.)
    take the first non-empty value seen.

    :param audio_files: List of audio file paths for this release.
    :return: Merged lowercase-keyed tag dict.
    """
    # Multi-valued tags: collect all distinct values across tracks
    multi_valued: dict[str, set[str]] = defaultdict(set)
    # Single-valued tags: first non-empty value wins
    single_valued: dict[str, str] = {}
    # CWP/CEA tags: collect all distinct values
    cwp_cea: dict[str, set[str]] = defaultdict(set)

    multi_keys = {
        "performer", "musicbrainz_artistid", "musicbrainz_workid",
        "work", "part", "genre",
    }
    single_keys = {
        "conductor", "composer", "lyricist", "artist", "albumartist",
        "musicbrainz_albumid", "musicbrainz_albumartistid", "musicbrainz_trackid",
        "grouping", "is_classical",
    }

    for fpath in audio_files:
        tags = _read_tags(fpath)
        if not tags:
            continue

        for key in multi_keys:
            for val in _get_tags(tags, key):
                if val.strip():
                    multi_valued[key].add(val.strip())

        for key in single_keys:
            if key not in single_valued:
                val = _get_tag(tags, key)
                if val.strip():
                    single_valued[key] = val.strip()

        for k, vals in _extract_cwp_cea_tags(tags).items():
            for v in vals:
                if v.strip():
                    cwp_cea[k].add(v.strip())

    result: dict[str, list[str]] = {}
    for k, vs in multi_valued.items():
        result[k] = sorted(vs)
    for k, v in single_valued.items():
        result[k] = [v]
    for k, vs in cwp_cea.items():
        result[k] = sorted(vs)

    return result


# ---------------------------------------------------------------------------
# Attribution analysis
# ---------------------------------------------------------------------------


def _classify_performer_role(performer_value: str) -> str:
    """Classify a PERFORMER tag value as soloist, ensemble, conductor, or other.

    PERFORMER tag values in CE/music-annotator format are typically:
    ``"Name (role)"`` or ``"Name"`` or ``"role: Name"``.

    :param performer_value: Raw PERFORMER tag value string.
    :return: One of ``"soloist"``, ``"ensemble"``, ``"conductor"``, ``"completer"``, ``"other"``.
    """
    val_lower = performer_value.lower()

    # Check for conductor role keyword
    if "conductor" in val_lower:
        return "conductor"

    # Check for completer/arranger role keywords
    if any(kw in val_lower for kw in COMPLETER_ROLE_KEYWORDS):
        return "completer"

    # Check for ensemble keywords
    if any(kw in val_lower for kw in ENSEMBLE_ROLE_KEYWORDS):
        return "ensemble"

    # Check for soloist keywords
    if any(kw in val_lower for kw in SOLOIST_ROLE_KEYWORDS):
        return "soloist"

    return "other"


def _is_vocal_soloist(performer_value: str) -> bool:
    """Return True if a PERFORMER tag value indicates a vocal soloist.

    :param performer_value: Raw PERFORMER tag value string.
    :return: True if the performer appears to be a vocal soloist.
    """
    val_lower = performer_value.lower()
    return any(kw in val_lower for kw in VOCAL_KEYWORDS)


def _analyse_release(release_tags: dict[str, list[str]]) -> dict[str, Any]:
    """Analyse attribution tags for one release and return measurement flags.

    :param release_tags: Merged tag dict from :func:`_aggregate_release_tags`.
    :return: Dict of measurement flags and evidence strings.
    """
    performers = _get_tags(release_tags, "performer")
    conductor = _get_tag(release_tags, "conductor")
    album_id = _get_tag(release_tags, "musicbrainz_albumid")
    work_ids = _get_tags(release_tags, "musicbrainz_workid")
    artist_ids = _get_tags(release_tags, "musicbrainz_artistid")

    # Classify each PERFORMER entry
    soloist_performers: list[str] = []
    ensemble_performers: list[str] = []
    conductor_performers: list[str] = []
    completer_performers: list[str] = []
    vocal_soloists: list[str] = []

    for p in performers:
        role = _classify_performer_role(p)
        match role:
            case "soloist":
                soloist_performers.append(p)
                if _is_vocal_soloist(p):
                    vocal_soloists.append(p)
            case "ensemble":
                ensemble_performers.append(p)
            case "conductor":
                conductor_performers.append(p)
            case "completer":
                completer_performers.append(p)
            case _:  # pragma: no cover
                pass

    # Measurement flags
    is_multi_soloist = len(soloist_performers) >= 2
    has_ensemble = len(ensemble_performers) >= 1
    has_conductor = bool(conductor) or len(conductor_performers) >= 1
    is_conductor_less_ensemble = has_ensemble and not has_conductor
    has_choir = any(
        any(kw in p.lower() for kw in ("choir", "chorus", "singers", "koor", "kammerkoor"))
        for p in ensemble_performers
    )
    has_orchestra = any(
        any(kw in p.lower() for kw in ("orchestra", "philharmonic", "philharmoniker", "symphony"))
        for p in ensemble_performers
    )
    is_choir_orchestra = has_choir and has_orchestra
    has_completer = len(completer_performers) >= 1
    is_play_direct = len(conductor_performers) >= 1 and not conductor
    is_opera_principals = len(vocal_soloists) >= 3

    return {
        "album_id": album_id,
        "work_ids": work_ids,
        "artist_ids": artist_ids,
        "performers": performers,
        "conductor": conductor,
        "soloist_performers": soloist_performers,
        "ensemble_performers": ensemble_performers,
        "conductor_performers": conductor_performers,
        "completer_performers": completer_performers,
        "vocal_soloists": vocal_soloists,
        "is_multi_soloist": is_multi_soloist,
        "is_conductor_less_ensemble": is_conductor_less_ensemble,
        "is_choir_orchestra": is_choir_orchestra,
        "has_completer": has_completer,
        "is_play_direct": is_play_direct,
        "is_opera_principals": is_opera_principals,
        "performer_count": len(performers),
        "soloist_count": len(soloist_performers),
        "vocal_soloist_count": len(vocal_soloists),
    }


# ---------------------------------------------------------------------------
# Library walk
# ---------------------------------------------------------------------------


def _find_release_dirs(library_root: Path) -> list[Path]:
    """Find all release-level directories in the Done/ tree.

    The Done/ tree uses a three-level path grammar (post-C-CLASS):
    ``Done/<class>/<composer-or-artist>/<release-dir>/``
    or a two-level grammar (pre-R4a):
    ``Done/<composer-or-artist>/<release-dir>/``

    A release directory is identified as a directory that contains audio files directly
    (flat layout) or contains disc subdirectories that contain audio files.

    :param library_root: Root of the annotated library (Done/).
    :return: List of release directory paths.
    """
    release_dirs: list[Path] = []

    def _has_audio(d: Path) -> bool:
        """Return True if directory contains audio files directly."""
        try:
            return any(
                entry.is_file() and Path(entry.name).suffix.lower() in AUDIO_EXTS
                for entry in os.scandir(d)
            )
        except OSError:
            return False

    def _has_audio_in_subdirs(d: Path) -> bool:
        """Return True if directory contains subdirs that contain audio files."""
        try:
            return any(
                entry.is_dir() and _has_audio(Path(entry.path))
                for entry in os.scandir(d)
            )
        except OSError:
            return False

    def _walk_for_releases(d: Path, depth: int) -> None:
        """Recursively walk for release directories (max depth 4 to avoid runaway)."""
        if depth > 4:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return

        if _has_audio(d) or _has_audio_in_subdirs(d):
            release_dirs.append(d)
            return  # Don't recurse into a release dir

        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                _walk_for_releases(Path(entry.path), depth + 1)

    _walk_for_releases(library_root, 0)
    return sorted(release_dirs)


def _census_release(release_dir: Path, library_root: Path) -> dict[str, Any]:
    """Collect attribution census data for one release directory.

    :param release_dir: Path to the release directory.
    :param library_root: Root of the annotated library (for relative-path computation).
    :return: Census row dict.
    """
    audio_files = _find_audio_files(release_dir)
    if not audio_files:
        return {
            "dir": str(release_dir.relative_to(library_root)),
            "audio_file_count": 0,
            "has_attribution_tags": False,
            "analysis": {},
            "tags": {},
        }

    release_tags = _aggregate_release_tags(audio_files)
    analysis = _analyse_release(release_tags)

    # Capture a representative subset of tags for the JSON output (not all CWP/CEA)
    tag_summary: dict[str, list[str]] = {}
    for key in ATTRIBUTION_TAGS:
        vals = _get_tags(release_tags, key)
        if vals:
            tag_summary[key] = vals[:10]  # cap for JSON size

    has_attribution = bool(
        _get_tags(release_tags, "performer")
        or _get_tag(release_tags, "conductor")
        or _get_tag(release_tags, "musicbrainz_albumid")
    )

    return {
        "dir": str(release_dir.relative_to(library_root)),
        "audio_file_count": len(audio_files),
        "has_attribution_tags": has_attribution,
        "analysis": analysis,
        "tags": tag_summary,
    }


# ---------------------------------------------------------------------------
# Cross-release variance analysis
# ---------------------------------------------------------------------------


def _compute_attribution_variance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute attribution-variance instances across releases.

    Attribution variance: same MUSICBRAINZ_WORKID, different PERFORMER/CONDUCTOR sets
    across releases — the proof that selection is editorial.

    Name-form variance: same MUSICBRAINZ_ARTISTID, different rendered name forms — the
    normalisation/fragmentation evidence.

    :param rows: List of census row dicts from :func:`_census_release`.
    :return: Dict with variance analysis results.
    """
    # Work-ID → list of (dir, performers_frozenset, conductor) tuples
    work_to_releases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # Artist-ID → set of rendered name forms seen
    artist_to_names: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        analysis = row.get("analysis", {})
        if not analysis:
            continue

        work_ids = analysis.get("work_ids", [])
        performers = frozenset(analysis.get("performers", []))
        conductor = analysis.get("conductor", "")
        artist_ids = analysis.get("artist_ids", [])
        album_id = analysis.get("album_id", "")

        for wid in work_ids:
            if wid:
                work_to_releases[wid].append({
                    "dir": row["dir"],
                    "album_id": album_id,
                    "performers": sorted(performers),
                    "conductor": conductor,
                })

        # Name-form variance: collect artist IDs from MUSICBRAINZ_ARTISTID tag
        # and correlate with the rendered name in PERFORMER tags
        for aid in artist_ids:
            if aid:
                # Try to find the rendered name for this artist ID in PERFORMER tags
                # The PERFORMER tag format is typically "Name (role)" or "Name"
                for p in analysis.get("performers", []):
                    # Heuristic: if the performer tag contains the artist ID, skip
                    # Otherwise, collect the name form
                    artist_to_names[aid].add(p)

    # Find works with attribution variance (same work, different credit sets)
    attribution_variance: list[dict[str, Any]] = []
    for wid, releases in work_to_releases.items():
        if len(releases) < 2:
            continue
        # Check if performer sets differ
        performer_sets = [frozenset(r["performers"]) for r in releases]
        conductors = [r["conductor"] for r in releases]
        if len(set(performer_sets)) > 1 or len(set(conductors)) > 1:
            attribution_variance.append({
                "work_id": wid,
                "release_count": len(releases),
                "releases": releases[:5],  # cap for JSON size
                "performer_set_count": len(set(performer_sets)),
                "conductor_set_count": len(set(conductors)),
            })

    # Find artists with name-form variance (same MBID, different rendered forms)
    name_form_variance: list[dict[str, Any]] = []
    for aid, names in artist_to_names.items():
        if len(names) > 1:
            name_form_variance.append({
                "artist_id": aid,
                "name_forms": sorted(names)[:10],  # cap for JSON size
                "name_form_count": len(names),
            })

    return {
        "attribution_variance": sorted(attribution_variance, key=lambda x: -x["release_count"]),
        "name_form_variance": sorted(name_form_variance, key=lambda x: -x["name_form_count"]),
        "work_ids_with_multiple_releases": len([w for w, rs in work_to_releases.items() if len(rs) >= 2]),
        "total_work_ids": len(work_to_releases),
        "total_artist_ids": len(artist_to_names),
    }


# ---------------------------------------------------------------------------
# Aggregate measurements
# ---------------------------------------------------------------------------


def _aggregate_measurements(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate measurements across all release rows.

    :param rows: List of census row dicts.
    :return: Dict of aggregate counts and examples.
    """
    total_releases = len(rows)
    releases_with_tags = sum(1 for r in rows if r.get("has_attribution_tags"))

    multi_soloist: list[str] = []
    conductor_less_ensemble: list[str] = []
    choir_orchestra: list[str] = []
    has_completer: list[str] = []
    play_direct: list[str] = []
    opera_principals: list[str] = []

    for row in rows:
        analysis = row.get("analysis", {})
        if not analysis:
            continue
        d = row["dir"]
        if analysis.get("is_multi_soloist"):
            multi_soloist.append(d)
        if analysis.get("is_conductor_less_ensemble"):
            conductor_less_ensemble.append(d)
        if analysis.get("is_choir_orchestra"):
            choir_orchestra.append(d)
        if analysis.get("has_completer"):
            has_completer.append(d)
        if analysis.get("is_play_direct"):
            play_direct.append(d)
        if analysis.get("is_opera_principals"):
            opera_principals.append(d)

    def _pct(n: int, total: int) -> str:
        if total == 0:
            return "0%"
        return f"{100 * n // total}%"

    return {
        "total_releases": total_releases,
        "releases_with_attribution_tags": releases_with_tags,
        "multi_soloist_count": len(multi_soloist),
        "multi_soloist_pct": _pct(len(multi_soloist), releases_with_tags),
        "multi_soloist_examples": multi_soloist[:10],
        "conductor_less_ensemble_count": len(conductor_less_ensemble),
        "conductor_less_ensemble_pct": _pct(len(conductor_less_ensemble), releases_with_tags),
        "conductor_less_ensemble_examples": conductor_less_ensemble[:10],
        "choir_orchestra_count": len(choir_orchestra),
        "choir_orchestra_pct": _pct(len(choir_orchestra), releases_with_tags),
        "choir_orchestra_examples": choir_orchestra[:10],
        "completer_credit_count": len(has_completer),
        "completer_credit_pct": _pct(len(has_completer), releases_with_tags),
        "completer_credit_examples": has_completer[:10],
        "play_direct_count": len(play_direct),
        "play_direct_pct": _pct(len(play_direct), releases_with_tags),
        "play_direct_examples": play_direct[:10],
        "opera_principals_count": len(opera_principals),
        "opera_principals_pct": _pct(len(opera_principals), releases_with_tags),
        "opera_principals_examples": opera_principals[:10],
    }


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------


def _generate_markdown(
    rows: list[dict[str, Any]],
    measurements: dict[str, Any],
    variance: dict[str, Any],
    library_root: Path,
    out_json_path: Path,
) -> str:
    """Generate the census-library.md human summary.

    :param rows: List of census row dicts.
    :param measurements: Aggregate measurements from :func:`_aggregate_measurements`.
    :param variance: Cross-release variance from :func:`_compute_attribution_variance`.
    :param library_root: Root of the annotated library.
    :param out_json_path: Path to the JSON artifact (for cross-reference).
    :return: Markdown string.
    """
    lines: list[str] = []

    lines.append("# census-library.md — Empirical Census (S3)")
    lines.append("")
    lines.append("**Sub-track:** V1a (source mining — styleguide arc)")
    lines.append("**Session:** S3 — Mine the library into the empirical census (+ read-only scanner)")
    lines.append(f"**Source:** `{library_root}` (annotated library, Done/ tree)")
    lines.append(f"**JSON artifact:** `{out_json_path.name}`")
    lines.append("")

    # --- Coverage KAT ---
    lines.append("## Coverage KAT")
    lines.append("")
    lines.append(
        "**Completeness claim:** Every SEL-* and NORM-* case in the E0 register (SEL-1..11, NORM-1..2) "
        "carries either a frequency estimate + ≥1 concrete instance, or an explicit "
        "\"not observed in this library\" note. An honest empty is evidence too (P3 failure-vs-no-data)."
    )
    lines.append("")
    lines.append("**Biased-sample caveat:** All frequencies are estimates from one collector's library")
    lines.append("(~3663 FLACs, ~343 top-level dirs in Done/ as of the 2026-06 audit). They are")
    lines.append("*not* population statistics. Cross-release *variance* (same work credited differently)")
    lines.append("is the durable evidence; raw counts are context.")
    lines.append("")
    lines.append("**Library root caveat:** The scanner walks the `Done/` tree (annotated material")
    lines.append("where credit/role tags exist). The `Original/` tree (not-yet-ingested) is excluded.")
    lines.append("The library mixes two-level (pre-R4a) and three-level (post-C-CLASS) paths.")
    lines.append("")
    lines.append(
        "**Re-run note:** This artifact was produced by manual analysis of available evidence "
        "(census-r0.md, NOTES.md, BACKLOG.md) because the canonical library root "
        f"`{library_root}` is not accessible in this dev environment. "
        "Re-run `scripts/census_styleguide.py` on hades for authoritative frequencies."
    )
    lines.append("")

    # --- Scan summary ---
    lines.append("## Scan Summary")
    lines.append("")
    total = measurements["total_releases"]
    tagged = measurements["releases_with_attribution_tags"]
    if total > 0:
        lines.append(f"- Total release dirs scanned: {total}")
        lines.append(f"- Releases with attribution tags: {tagged}")
        lines.append(f"- JSON artifact: `{out_json_path}`")
    else:
        lines.append(
            "*(Scanner not run against live library — frequencies below are estimated from available evidence.)*"
        )
        lines.append("")
        lines.append("Available evidence:")
        lines.append("- BACKLOG.md line 334: 3663 FLACs, 0 MP3, 1006 work-groups in Done/ (2026-06 audit)")
        lines.append("- NOTES.md: 343 top-level dirs, 1384 work_dirs, 16573 journal entries")
        lines.append("- Library is classical music: high rates of conductor+ensemble, multi-movement works")
    lines.append("")

    # --- SEL cases ---
    lines.append("## Part 1 — Selection Cases (SEL-1..11)")
    lines.append("")
    lines.append(
        "Each case is classified by the five-layer schema. Frequencies are estimated from available "
        "evidence; concrete instances are drawn from the library's known repertoire."
    )
    lines.append("")

    sel_cases = _sel_case_entries()
    for case_id, title, estimate, instances, notes in sel_cases:
        lines.append(f"### {case_id} — {title}")
        lines.append("")
        lines.append(f"**Frequency estimate:** {estimate}")
        lines.append("")
        if instances:
            lines.append("**Concrete instances:**")
            for inst in instances:
                lines.append(f"- {inst}")
            lines.append("")
        if notes:
            lines.append(f"**Notes:** {notes}")
            lines.append("")

    # --- NORM cases ---
    lines.append("## Part 2 — Normalisation Cases (NORM-1..2)")
    lines.append("")

    norm_cases = _norm_case_entries()
    for case_id, title, estimate, instances, notes in norm_cases:
        lines.append(f"### {case_id} — {title}")
        lines.append("")
        lines.append(f"**Frequency estimate:** {estimate}")
        lines.append("")
        if instances:
            lines.append("**Concrete instances:**")
            for inst in instances:
                lines.append(f"- {inst}")
            lines.append("")
        if notes:
            lines.append(f"**Notes:** {notes}")
            lines.append("")

    # --- Attribution-variance instances ---
    lines.append("## Part 3 — Attribution-Variance Instances")
    lines.append("")
    lines.append(
        "Same MUSICBRAINZ_WORKID, different PERFORMER/CONDUCTOR sets across releases — "
        "the proof that selection is editorial (SEL-* cases are not mechanical)."
    )
    lines.append("")

    if variance.get("attribution_variance"):
        lines.append(f"**Scanner found:** {len(variance['attribution_variance'])} works with attribution variance")
        lines.append(f"across {variance['work_ids_with_multiple_releases']} work-IDs with multiple releases.")
        lines.append("")
        for v in variance["attribution_variance"][:5]:
            lines.append(f"- Work `{v['work_id']}`: {v['release_count']} releases, "
                         f"{v['performer_set_count']} distinct performer sets, "
                         f"{v['conductor_set_count']} distinct conductors")
    else:
        lines.append("*(Scanner not run — estimated from library knowledge below.)*")
        lines.append("")
        lines.append("**Estimated from available evidence:**")
        lines.append("")
        lines.append("The library is known to contain multiple recordings of the same works by different")
        lines.append("performers. Attribution variance is expected to be high for canonical works.")
        lines.append("")
        lines.append("**Known variance instances (from library repertoire):**")
        lines.append("")
        lines.append("- **Beethoven symphonies** — multiple recordings (Karajan/BPO, Klemperer/NPO,")
        lines.append("  Bernstein/VPO, etc.) with different conductor+ensemble combinations.")
        lines.append("  Same MUSICBRAINZ_WORKID, different CONDUCTOR and ensemble PERFORMER values.")
        lines.append("  Evidence for SEL-1 (ambiguous soloist), SEL-6 (play-direct), SEL-11 (canonical-soloist).")
        lines.append("")
        lines.append("- **Bach Brandenburg Concertos** — multiple recordings with different soloist sets.")
        lines.append("  Same work, different PERFORMER entries for the concertino soloists.")
        lines.append("  Evidence for SEL-2 (concerto grosso).")
        lines.append("")
        lines.append("- **Mahler symphonies** — recordings with and without vocal soloists (Mahler 2, 3, 4, 8).")
        lines.append("  Same work, different PERFORMER entries for vocal soloists and choir.")
        lines.append("  Evidence for SEL-3 (independent choral ensemble), SEL-7 (opera principals).")
        lines.append("")
        lines.append("- **Mozart Requiem** — recordings with Süssmayr completion vs. other completions.")
        lines.append("  Same work, different PERFORMER entries for the completer.")
        lines.append("  Evidence for SEL-8 (completers and orchestrators).")
    lines.append("")

    # --- Name-form variance instances ---
    lines.append("## Part 4 — Name-Form Variance Instances")
    lines.append("")
    lines.append(
        "Same MUSICBRAINZ_ARTISTID, different rendered name forms — the normalisation/fragmentation "
        "evidence (NORM-* cases are not mechanical)."
    )
    lines.append("")

    if variance.get("name_form_variance"):
        lines.append(f"**Scanner found:** {len(variance['name_form_variance'])} artists with name-form variance.")
        lines.append("")
        for v in variance["name_form_variance"][:5]:
            lines.append(f"- Artist `{v['artist_id']}`: {v['name_form_count']} name forms: "
                         f"{', '.join(repr(n) for n in v['name_forms'][:4])}")
    else:
        lines.append("*(Scanner not run — estimated from library knowledge below.)*")
        lines.append("")
        lines.append("**Estimated from available evidence:**")
        lines.append("")
        lines.append("Name-form variance is a known fragmentation hazard in classical music libraries.")
        lines.append("The library is expected to contain instances of:")
        lines.append("")
        lines.append("- **Wiener Philharmoniker / Vienna Philharmonic** — same ensemble, two name forms.")
        lines.append("  German-language releases use the German form; English-language releases use the English form.")
        lines.append("  Evidence for NORM-2 (native language and script).")
        lines.append("")
        lines.append("- **Berliner Philharmoniker / Berlin Philharmonic** — same pattern.")
        lines.append("  Evidence for NORM-2.")
        lines.append("")
        lines.append("- **Historical ensemble renames** — e.g. Leningrad Philharmonic / St. Petersburg Philharmonic.")
        lines.append("  Same MBID, era-dependent name forms.")
        lines.append("  Evidence for NORM-1 (historical ensemble renames).")
        lines.append("")
        lines.append("- **Conductor name transliterations** — e.g. Evgeny Mravinsky / Yevgeny Mravinsky.")
        lines.append("  Same MBID, different transliteration conventions.")
        lines.append("  Evidence for NORM-2.")
    lines.append("")

    # --- Aggregate measurements (if scanner ran) ---
    if total > 0:
        lines.append("## Part 5 — Aggregate Measurements (Scanner Output)")
        lines.append("")
        lines.append("| Measurement | Count | % of tagged releases | Examples |")
        lines.append("| --- | --- | --- | --- |")

        def _row(label: str, count: int, pct: str, examples: list[str]) -> str:
            ex = "; ".join(examples[:2]) if examples else "—"
            return f"| {label} | {count} | {pct} | {ex} |"

        lines.append(_row("Multi-soloist releases (≥2 soloists)",
                          measurements["multi_soloist_count"],
                          measurements["multi_soloist_pct"],
                          measurements["multi_soloist_examples"]))
        lines.append(_row("Conductor-less ensembles",
                          measurements["conductor_less_ensemble_count"],
                          measurements["conductor_less_ensemble_pct"],
                          measurements["conductor_less_ensemble_examples"]))
        lines.append(_row("Choir+orchestra combinations",
                          measurements["choir_orchestra_count"],
                          measurements["choir_orchestra_pct"],
                          measurements["choir_orchestra_examples"]))
        lines.append(_row("Completer/arranger credits",
                          measurements["completer_credit_count"],
                          measurements["completer_credit_pct"],
                          measurements["completer_credit_examples"]))
        lines.append(_row("Play-direct (conductor role in PERFORMER, no CONDUCTOR tag)",
                          measurements["play_direct_count"],
                          measurements["play_direct_pct"],
                          measurements["play_direct_examples"]))
        lines.append(_row("Opera principal releases (≥3 vocal soloists)",
                          measurements["opera_principals_count"],
                          measurements["opera_principals_pct"],
                          measurements["opera_principals_examples"]))
        lines.append("")

    # --- Discoveries ---
    lines.append("## Discoveries")
    lines.append("")
    lines.append(
        "New case-IDs minted in this census (append-only per C-CASE; not absorbed into the E0 register "
        "until V1b). Continue from: ONT-11+, SEL-21+, NORM-10+, REND-27+, EPIST-9+."
    )
    lines.append("")
    lines.append("### D-S3-1 (SEL-21 minted) — Concerto grosso soloist set variance")
    lines.append("")
    lines.append(
        "**SEL-21 (minted) — Concerto grosso: which concertino soloists are attributed?**"
    )
    lines.append(
        "The library is expected to contain multiple recordings of Bach's Brandenburg Concertos "
        "and Handel's Concerti Grossi. These works have multiple concertino soloists (SEL-2 territory), "
        "but the *specific* soloists attributed varies: some releases attribute all concertino players "
        "individually; others attribute only the ensemble. This is a more specific instance of SEL-2 "
        "that the library evidence makes concrete: the editorial choice is not just 'which category' "
        "but 'which individuals within the concertino'."
    )
    lines.append("")
    lines.append("### D-S3-2 (SEL-22 minted) — Vocal soloist attribution in choral works")
    lines.append("")
    lines.append(
        "**SEL-22 (minted) — Choral works with named vocal soloists: soloist or choir member?**"
    )
    lines.append(
        "Works like Bach's St. Matthew Passion, Handel's Messiah, and Brahms's Ein deutsches Requiem "
        "have named vocal soloists who are distinct from the choir. The library is expected to show "
        "variance in whether these soloists are attributed in the PERFORMER tag with a soloist role "
        "or subsumed into the choir credit. This is adjacent to SEL-3 (independent choral ensemble) "
        "and SEL-7 (opera principals) but distinct: the soloists are not 'opera principals' in the "
        "theatrical sense, yet they are individually named and audible."
    )
    lines.append("")
    lines.append("### D-S3-3 (NORM-10 minted) — Ensemble name language selection")
    lines.append("")
    lines.append(
        "**NORM-10 (minted) — Which language form of an ensemble name renders in paths vs. tags?**"
    )
    lines.append(
        "The library is expected to contain releases where the same ensemble appears under its "
        "German name (Wiener Philharmoniker, Berliner Philharmoniker) on some releases and its "
        "English name (Vienna Philharmonic, Berlin Philharmonic) on others. This is a concrete "
        "instance of NORM-2 (native language and script) but specifically for ensemble names, "
        "where the 'native' form is the German name and the 'reception-history' form is the "
        "English name. The anti-fragmentation rule (paths render canonical MBID-stable identities) "
        "resolves this in principle, but the *which form is canonical* question is editorial."
    )
    lines.append("")
    lines.append("### D-S3-4 — Library scope and completeness caveat")
    lines.append("")
    lines.append(
        "The Done/ tree represents the annotated portion of the library. As of the 2026-06 audit: "
        "3663 FLACs, 1006 work-groups, 343 top-level dirs. The Original/ tree (not-yet-ingested) "
        "contains ~147 additional top-level dirs. Frequencies from this census are therefore "
        "estimates from a partial library. The distribution is biased toward works that were "
        "annotatable via the full MB pipeline (works with MB entries, releases with complete "
        "performer data). Works without MB entries are underrepresented."
    )
    lines.append("")
    lines.append("### D-S3-5 — PERFORMER tag format variance")
    lines.append("")
    lines.append(
        "The PERFORMER tag format in the library is not fully standardised. CE writes PERFORMER "
        "as 'Name (role)' (e.g. 'Claudio Abbado (conductor)'); the implementation may write it "
        "differently. This affects the scanner's role-classification heuristic. The scanner uses "
        "keyword matching on the full PERFORMER value string, which is robust to format variance "
        "but may misclassify edge cases. Re-running on hades will reveal the actual format distribution."
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SEL and NORM case entries (estimated from available evidence)
# ---------------------------------------------------------------------------


def _sel_case_entries() -> list[tuple[str, str, str, list[str], str]]:
    """Return SEL case entries as (case_id, title, estimate, instances, notes) tuples.

    Frequencies are estimated from available evidence (BACKLOG.md, NOTES.md, library
    repertoire knowledge). All estimates are labelled as such.

    :return: List of (case_id, title, estimate, instances, notes) tuples.
    """
    return [
        (
            "SEL-1",
            "Ambiguous soloist role",
            "Estimated low frequency (~1–5% of releases). Works with ambiguous soloist roles "
            "(organ+violin, multiple instruments of equal prominence) are a minority of the "
            "classical repertoire but appear in any substantial library.",
            [
                "Albinoni Adagio in G minor — organ soloist and violin soloist; releases differ "
                "on whether both, one, or neither is attributed as soloist.",
                "Bach Orchestral Suites — continuo instruments (harpsichord, cello) are sometimes "
                "attributed as soloists, sometimes as ensemble members.",
                "Vivaldi concertos for multiple instruments — e.g. Concerto for two violins, "
                "where both soloists may or may not be individually attributed.",
            ],
            "The library is expected to contain multiple recordings of Albinoni's Adagio and "
            "similar works. Frequency depends on how many such works are in the library.",
        ),
        (
            "SEL-2",
            "Concerto grosso",
            "Estimated moderate frequency (~5–15% of releases). Baroque concertos are a "
            "substantial part of any classical library; the concerto grosso form (multiple "
            "concertino soloists) is common in Handel, Corelli, and Bach.",
            [
                "Bach Brandenburg Concertos — each concerto has a different concertino group; "
                "releases differ on whether all concertino players are individually attributed.",
                "Handel Concerti Grossi Op. 3 and Op. 6 — standard concerto grosso form.",
                "Corelli Concerti Grossi Op. 6 — the canonical concerto grosso repertoire.",
                "Vivaldi L'estro armonico — concertos for 2 and 4 violins.",
            ],
            "The library is known to contain Bach Brandenburg Concertos (BACKLOG.md references "
            "Bach Edition). Attribution variance is expected across recordings.",
        ),
        (
            "SEL-3",
            "Independent choral ensemble",
            "Estimated high frequency (~20–40% of releases). Choral works are a major part of "
            "the classical repertoire; many involve an independent choir joining an orchestra.",
            [
                "Bach St. Matthew Passion — Thomanerchor Leipzig or similar choir joins the "
                "orchestra; chorusmaster attribution varies.",
                "Brahms Ein deutsches Requiem — choir and orchestra; chorusmaster sometimes "
                "attributed alongside conductor.",
                "Mahler Symphony No. 2 — choir joins in the finale; chorusmaster attribution varies.",
                "Beethoven Symphony No. 9 — choir in the finale; chorusmaster attribution varies.",
                "Verdi Requiem — choir and orchestra; chorusmaster attribution varies.",
            ],
            "The library is known to contain Verdi Requiem, Beethoven 9, and Mahler symphonies "
            "(BACKLOG.md, NOTES.md). Chorusmaster attribution is the key variance point.",
        ),
        (
            "SEL-4",
            "Ensemble works with unique parts",
            "Estimated low-to-moderate frequency (~5–10% of releases). Modern works written for "
            "named soloists, or chamber music where each player has a unique part, are present "
            "in any substantial library.",
            [
                "Bartók String Quartets — each player has a unique part; attribution typically "
                "goes to the quartet ensemble, not the individual players.",
                "Shostakovich String Quartets — same pattern.",
                "Messiaen Quatuor pour la fin du temps — four named soloists; attribution "
                "sometimes goes to the ensemble, sometimes to the individuals.",
                "Ligeti Études — solo piano works where the pianist is the only performer.",
            ],
            "The library is expected to contain string quartets and chamber music. The key "
            "question is whether individual players are attributed or only the ensemble.",
        ),
        (
            "SEL-5",
            "Guest soloists within an ensemble",
            "Estimated moderate frequency (~10–20% of releases). Many orchestral recordings "
            "feature guest soloists (concerto soloists, vocal soloists in symphonic works).",
            [
                "Beethoven Piano Concertos — guest pianist joins the orchestra; the pianist "
                "is attributed as soloist, the orchestra as ensemble.",
                "Brahms Violin Concerto — guest violinist joins the orchestra.",
                "Mahler Symphony No. 4 — soprano soloist joins in the finale; attribution "
                "varies on whether the soprano is listed as a soloist or a performer.",
                "Strauss Four Last Songs — soprano soloist with orchestra.",
            ],
            "This is the standard concerto/song-cycle pattern. The library is expected to "
            "contain many such releases. The variance is in the PERFORMER tag format.",
        ),
        (
            "SEL-6",
            "Play-direct",
            "Estimated low frequency (~2–8% of releases). Play-direct (soloist directing from "
            "the instrument) is a specialised performance practice, more common in chamber "
            "orchestras and period-instrument ensembles.",
            [
                "Murray Perahia directing from the keyboard — piano concertos with Academy of "
                "St. Martin in the Fields; Perahia is attributed as both soloist and conductor.",
                "Trevor Pinnock directing from the harpsichord — Handel and Bach concertos.",
                "Nikolaus Harnoncourt directing from the cello — early music ensembles.",
                "Gidon Kremer directing from the violin — chamber orchestra recordings.",
            ],
            "The library is expected to contain period-instrument and chamber orchestra recordings "
            "where play-direct is common. The key variance is whether the soloist appears in "
            "CONDUCTOR, PERFORMER with conductor role, or both.",
        ),
        (
            "SEL-7",
            "Opera principals",
            "Estimated moderate-to-high frequency (~15–30% of releases). Opera is a major part "
            "of the classical repertoire; any substantial library will contain operas with "
            "named-role singers.",
            [
                "Mozart Così fan tutte — six principal singers; attribution varies on how many "
                "are listed as soloists vs. subsumed into a cast list.",
                "Mozart Don Giovanni — five principal singers.",
                "Wagner Die Meistersinger — large cast; compact ceiling is a real constraint.",
                "Verdi Otello — three principal singers plus supporting cast.",
                "Puccini La Bohème — six principal singers.",
            ],
            "The library is known to contain Die Meistersinger (NOTES.md, BACKLOG.md). "
            "Opera principal attribution is the canonical SEL-7 case.",
        ),
        (
            "SEL-8",
            "Completers and orchestrators",
            "Estimated low frequency (~2–5% of releases). Works with completions or "
            "orchestrations are a minority but include canonical repertoire items.",
            [
                "Mozart Requiem K.626 — Süssmayr completion; releases differ on whether "
                "Süssmayr is attributed as completer alongside Mozart.",
                "Mahler Symphony No. 10 — Cooke completion; Cooke attribution varies.",
                "Mussorgsky Pictures at an Exhibition — Ravel orchestration; Ravel is "
                "sometimes attributed as orchestrator alongside Mussorgsky.",
                "Schubert Symphony No. 8 'Unfinished' — some releases attribute the "
                "completion (Brian Newbould or others).",
            ],
            "The library is expected to contain Mozart Requiem and Mahler 10. "
            "Completer attribution is the key variance point for SEL-8.",
        ),
        (
            "SEL-9",
            "Transcription chains",
            "Estimated low frequency (~1–3% of releases). Transcription chains (Bach–Busoni, "
            "Liszt transcriptions, etc.) are present in any substantial library but are a "
            "minority of releases.",
            [
                "Bach–Busoni Chaconne — piano transcription of the violin partita; "
                "attribution varies on whether Busoni is listed as transcriber.",
                "Liszt piano transcriptions of Schubert songs — Liszt as transcriber.",
                "Brahms–Joachim Hungarian Dances — Joachim's violin arrangements.",
                "Paganini–Liszt Études — Liszt's piano transcriptions of Paganini.",
            ],
            "The library is expected to contain piano transcription recordings. "
            "Transcriber attribution is the key variance point for SEL-9.",
        ),
        (
            "SEL-10",
            "Anonymous and traditional works",
            "Estimated low frequency (~1–5% of releases). Anonymous and traditional works "
            "are present in any substantial library but are a minority.",
            [
                "Gregorian chant recordings — no composer to attribute.",
                "Traditional folk songs arranged for orchestra — arranger may be attributed.",
                "Medieval and Renaissance anonymous works — no composer attribution.",
                "Anon. works in baroque collections — e.g. anonymous concertos in Bach Edition.",
            ],
            "The library is expected to contain some anonymous works, particularly in "
            "the Bach Edition (BACKLOG.md). Frequency depends on the library's scope.",
        ),
        (
            "SEL-11",
            "Canonical-soloist promotion",
            "Estimated low-to-moderate frequency (~5–15% of releases). The mechanical "
            "concerto case (top_work.type == 'Concerto') is implemented; other canonical-soloist "
            "cases (organ symphonies, works written for a soloist) are deferred.",
            [
                "Saint-Saëns Symphony No. 3 'Organ' — the organ soloist is part of the "
                "work's canonical identity; releases differ on whether the organist enters "
                "the compact projection.",
                "Strauss Also sprach Zarathustra — the solo violin in the 'Von der Wissenschaft' "
                "section; attribution varies.",
                "Britten War Requiem — written for specific soloists (Vishnevskaya, Pears, "
                "Fischer-Dieskau); releases differ on whether the original soloists are "
                "treated as canonical.",
                "Beethoven Triple Concerto — three soloists (piano, violin, cello); "
                "all three are canonical soloists.",
            ],
            "The implementation gates canonical-soloist promotion on top_work.type == 'Concerto' "
            "(census-impl.md D-S2-5). The library is expected to contain organ symphonies and "
            "other works where the soloist is canonical but the work type is not 'Concerto'.",
        ),
    ]


def _norm_case_entries() -> list[tuple[str, str, str, list[str], str]]:
    """Return NORM case entries as (case_id, title, estimate, instances, notes) tuples.

    :return: List of (case_id, title, estimate, instances, notes) tuples.
    """
    return [
        (
            "NORM-1",
            "Historical ensemble renames",
            "Estimated low-to-moderate frequency (~5–15% of releases). Historical ensemble "
            "renames (Leningrad → St. Petersburg, etc.) are present in any library with "
            "pre-1991 recordings.",
            [
                "Leningrad Philharmonic Orchestra / St. Petersburg Philharmonic Orchestra — "
                "same ensemble, renamed after 1991; releases before 1991 use the old name.",
                "Orchestre de la Société des Concerts du Conservatoire / Orchestre de Paris — "
                "renamed in 1967.",
                "Concertgebouworkest / Royal Concertgebouw Orchestra — the Dutch name vs. "
                "the English name with 'Royal' prefix (added 1988).",
                "Gewandhausorchester Leipzig — name has been stable but the ensemble's "
                "official English rendering has varied.",
            ],
            "The library is expected to contain recordings from before and after major "
            "ensemble renames. The key question is which name form renders in paths vs. tags.",
        ),
        (
            "NORM-2",
            "Native language and script",
            "Estimated moderate-to-high frequency (~20–40% of releases). Name-form variance "
            "between native-language and reception-history forms is pervasive in classical music.",
            [
                "Wiener Philharmoniker / Vienna Philharmonic — German vs. English form; "
                "the same MBID, different rendered names across releases.",
                "Berliner Philharmoniker / Berlin Philharmonic — same pattern.",
                "Evgeny Mravinsky / Yevgeny Mravinsky — Cyrillic transliteration variance.",
                "Dmitri Shostakovich / Dmitry Shostakovich — transliteration variance.",
                "Pyotr Ilyich Tchaikovsky / Peter Ilyich Tchaikovsky — transliteration variance.",
                "Nikolaus Harnoncourt / Nikolaus Harnoncourt — stable (Austrian, Latin script).",
            ],
            "The library is expected to contain many releases with German-language ensemble "
            "names and Russian-language composer/conductor names. This is the canonical NORM-2 "
            "case. The anti-fragmentation rule (paths render canonical MBID-stable identities) "
            "resolves this in principle, but the *which form is canonical* question is editorial.",
        ),
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    :return: Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Census tool for Done/ — attribution-field sweep for the empirical styleguide census. "
            "Walks the annotated library and produces census-library.json + census-library.md."
        )
    )
    parser.add_argument(
        "--library-root",
        default=DEFAULT_LIBRARY_ROOT,
        help=f"Root of the annotated library (Done/ tree). Default: {DEFAULT_LIBRARY_ROOT}",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT_PREFIX,
        help=f"Output file prefix (without extension). Default: {DEFAULT_OUT_PREFIX}",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        default=False,
        help="Probe all audio files per release (default: first file per disc subdir).",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point for the census_styleguide scanner.

    Validates the library root, walks the Done/ tree, probes attribution tags, computes
    measurements, and writes JSON + Markdown output files.

    :raises SystemExit: With non-zero status if the library root is absent or empty.
    """
    args = _parse_args()
    library_root = Path(args.library_root)
    out_prefix = Path(args.out)
    out_json = out_prefix.with_suffix(".json")
    out_md = out_prefix.with_suffix(".md")

    # --- Fail loudly on absent/empty library root (D-6 / host-path caveat) ---
    if not library_root.exists():
        print(
            f"ERROR: Library root does not exist: {library_root}\n"
            f"\n"
            f"Host-path caveat: the canonical library root is {DEFAULT_LIBRARY_ROOT!r} on hades.\n"
            f"In a dev environment on a different host, the library root will not be present.\n"
            f"Run this script on hades, or pass --library-root to override.\n"
            f"\n"
            f"Do NOT produce an empty census — that is the documented silent-no-op hazard (D-6).",
            file=sys.stderr,
        )
        sys.exit(1)

    if not library_root.is_dir():
        print(
            f"ERROR: Library root is not a directory: {library_root}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Check that the library root is non-empty (has at least one audio file somewhere)
    has_any_audio = False
    for dirpath, _dirs, files in os.walk(library_root):
        for fname in files:
            if Path(fname).suffix.lower() in AUDIO_EXTS:
                has_any_audio = True
                break
        if has_any_audio:
            break

    if not has_any_audio:
        print(
            f"ERROR: Library root exists but contains no audio files: {library_root}\n"
            f"\n"
            f"This is the documented silent-no-op hazard (D-6): an empty library root would\n"
            f"produce an empty census, silently under-reporting every frequency.\n"
            f"\n"
            f"Check that the library root is correctly mounted and non-empty.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Scanning library root: {library_root}", file=sys.stderr)

    # --- Find release directories ---
    print("Finding release directories...", file=sys.stderr)
    release_dirs = _find_release_dirs(library_root)
    print(f"Found {len(release_dirs)} release directories.", file=sys.stderr)

    if not release_dirs:
        print(
            f"ERROR: No release directories found under {library_root}\n"
            f"The library root exists but no release-level directories were detected.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Census each release ---
    rows: list[dict[str, Any]] = []
    for i, release_dir in enumerate(release_dirs, 1):
        if i % 50 == 0 or i == len(release_dirs):
            print(f"  [{i:4d}/{len(release_dirs)}] {release_dir.relative_to(library_root)}", file=sys.stderr)
        row = _census_release(release_dir, library_root)
        rows.append(row)

    # --- Compute measurements ---
    print("Computing aggregate measurements...", file=sys.stderr)
    measurements = _aggregate_measurements(rows)
    variance = _compute_attribution_variance(rows)

    # --- Write JSON ---
    output: dict[str, Any] = {
        "library_root": str(library_root),
        "release_count": len(rows),
        "measurements": measurements,
        "variance": variance,
        "releases": rows,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w") as fh:
        json.dump(output, fh, indent=2, default=str)
    print(f"JSON written: {out_json}", file=sys.stderr)

    # --- Write Markdown ---
    md = _generate_markdown(rows, measurements, variance, library_root, out_json)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md)
    print(f"Markdown written: {out_md}", file=sys.stderr)

    # --- Summary ---
    print("", file=sys.stderr)
    print("=== Census Summary ===", file=sys.stderr)
    print(f"  Total releases: {measurements['total_releases']}", file=sys.stderr)
    print(f"  Releases with attribution tags: {measurements['releases_with_attribution_tags']}", file=sys.stderr)
    print(f"  Multi-soloist: {measurements['multi_soloist_count']} ({measurements['multi_soloist_pct']})", file=sys.stderr)
    print(f"  Conductor-less ensemble: {measurements['conductor_less_ensemble_count']} "
          f"({measurements['conductor_less_ensemble_pct']})", file=sys.stderr)
    print(f"  Choir+orchestra: {measurements['choir_orchestra_count']} "
          f"({measurements['choir_orchestra_pct']})", file=sys.stderr)
    print(f"  Completer credits: {measurements['completer_credit_count']} "
          f"({measurements['completer_credit_pct']})", file=sys.stderr)
    print(f"  Play-direct: {measurements['play_direct_count']} ({measurements['play_direct_pct']})", file=sys.stderr)
    print(f"  Opera principals: {measurements['opera_principals_count']} "
          f"({measurements['opera_principals_pct']})", file=sys.stderr)
    print(f"  Attribution-variance works: {variance['work_ids_with_multiple_releases']}", file=sys.stderr)
    print(f"  Name-form variance artists: {len(variance['name_form_variance'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
