"""music_annotator — Copy and tag a classical music album using MusicBrainz metadata.

Implements the Classical Extras Picard plugin conventions
(github.com/metabrainz/picard-plugins/tree/2.0/plugins/classical_extras).

Directory layout produced::

    <dest>/
      <Composer sort-name> - <Performers>/
        <Work title> (<work MBID>)/
          <nn> - <movement title>.<ext>

Tags written (Vorbis Comments for FLAC, ID3v2.4 for MP3):

    Standard Picard tags:
        TITLE, ARTIST, ARTISTS, ARTISTSORT, ALBUMARTIST, ALBUMARTISTSORT,
        ALBUM, TRACKNUMBER, TOTALTRACKS, DISCNUMBER, DATE, ORIGINALDATE,
        COMPOSER, COMPOSERSORT, CONDUCTOR, LYRICIST, ARRANGER, PERFORMER,
        ENSEMBLE, SOLOISTS, BAND, LABEL, ORGANIZATION, CATALOGNUMBER, BARCODE,
        MEDIA, SCRIPT, LANGUAGE, RELEASETYPE, RELEASESTATUS, GENRE,
        WORK, GROUPHEADING, TOP_WORK, PART, MOVEMENT, MOVEMENTNUMBER, MOVEMENTTOTAL,
        KEY, IS_CLASSICAL

    MusicBrainz ID tags (Picard-standard):
        MUSICBRAINZ_ALBUMID, MUSICBRAINZ_TRACKID, MUSICBRAINZ_RECORDINGID,
        MUSICBRAINZ_RELEASEGROUPID, MUSICBRAINZ_ARTISTID, MUSICBRAINZ_ALBUMARTISTID,
        MUSICBRAINZ_WORKID, MUSICBRAINZ_CONDUCTORID, MUSICBRAINZ_COMPOSERID

    Classical Extras _cwp_ variables (stored as tags, prefix CWP_):
        CWP_WORK_0 … CWP_WORK_N, CWP_WORKID_0 … CWP_WORKID_N
        CWP_WORK_TOP, CWP_WORKID_TOP
        CWP_PART_0 … CWP_PART_N
        CWP_PART_LEVELS, CWP_SINGLE_WORK_ALBUM
        CWP_WORK, CWP_GROUPHEADING, CWP_PART, CWP_INTER_WORK
        CWP_MOVT_NUM, CWP_MOVT_TOT
        CWP_COMPOSERS, CWP_COMPOSERS_SORT, CWP_COMPOSER_LASTNAMES
        CWP_ARRANGERS, CWP_ORCHESTRATORS, CWP_RECONSTRUCTORS, CWP_REVISORS
        CWP_LYRICISTS, CWP_LIBRETTISTS, CWP_TRANSLATORS
        CWP_KEYS, CWP_COMPOSED_DATES, CWP_PUBLISHED_DATES, CWP_PREMIERED_DATES

    Classical Extras _cea_ variables (stored as tags, prefix CEA_):
        CEA_RECORDING_ARTIST, CEA_RECORDING_ARTISTS
        CEA_SOLOISTS, CEA_SOLOIST_NAMES
        CEA_VOCALISTS, CEA_INSTRUMENTALISTS, CEA_OTHER_SOLOISTS
        CEA_ENSEMBLES, CEA_ENSEMBLE_NAMES
        CEA_CONDUCTORS, CEA_COMPOSERS, CEA_COMPOSER_LASTNAMES, CEA_PERFORMERS
        CEA_ARRANGERS, CEA_ORCHESTRATORS, CEA_CHORUSMASTERS, CEA_LEADERS
        CEA_INSTRUMENTS, CEA_INSTRUMENTS_CREDITED

Usage::

    python -m music_annotator \\
        --release-id  53c4d36c-1032-4f78-baba-fc972249d7d1 \\
        --src-dir "/path/to/source/album" \\
        --dest-dir /tmp/music_library \\
        [--user-agent "MyApp/1.0 contact@example.com"] \\
        [--dry-run] [--no-fetch-rels]
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import ParamSpec, TypeVar

import musicbrainzngs as mb
import structlog
from mutagen.flac import FLAC
from mutagen.flac import Picture as FLACPicture
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TCOM,
    TDOR,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPE3,
    TPOS,
    TPUB,
    TRCK,
    TXXX,
)
from mutagen.mp3 import MP3

from music_annotator.models import (
    JSON,
    ArtistEntry,
    CeaPerformers,
    CoverArt,
    CwpTags,
    MBArtistCredit,
    MBAttribute,
    MBRecording,
    MBRelease,
    MBTrack,
    MBWork,
    RoleBuckets,
    TrackTags,
    WorkDates,
    WorkHierarchyLevel,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

__all__ = [
    "init_mb",
    "fetch_release",
    "fetch_recording_detail",
    "fetch_cover_art",
    "fetch_work_detail",
    "is_ensemble",
    "is_choir",
    "is_orchestra",
    "artist_credit_phrase",
    "artist_ids",
    "artist_sort_names",
    "last_name",
    "build_work_hierarchy",
    "strip_common_prefix",
    "period_for_year",
    "extract_work_artist_rels",
    "collect_work_dates",
    "parse_year",
    "collect_work_tags_and_key",
    "build_cea_performers",
    "build_cwp_tags",
    "build_track_tags",
    "safe_name",
    "build_dest_path",
    "apply_tags_flac",
    "apply_tags_mp3",
    "find_source_files",
    "run",
]

# ---------------------------------------------------------------------------
# Constants mirroring Classical Extras defaults
# ---------------------------------------------------------------------------

#: Substrings identifying orchestral ensembles (from CEA_ORCHESTRAS).
ORCHESTRA_STRINGS: frozenset[str] = frozenset(
    {
        "orchestra",
        "philharmonic",
        "philharmonica",
        "philharmoniker",
        "musicians",
        "academy",
        "symphony",
        "orkester",
    }
)

#: Substrings identifying choral ensembles (from CEA_CHOIRS).
CHOIR_STRINGS: frozenset[str] = frozenset({"choir", "chorus", "singers", "domchor", "koor", "kammerkoor"})

#: Substrings identifying chamber/small-group ensembles (from CEA_GROUPS).
GROUP_STRINGS: frozenset[str] = frozenset(
    {
        "ensemble",
        "band",
        "trio",
        "quartet",
        "quintet",
        "sextet",
        "septet",
        "octet",
        "chamber",
        "consort",
        "players",
        "quartett",
    }
)

#: Union of all ensemble-identifying substrings.
ENSEMBLE_STRINGS: frozenset[str] = ORCHESTRA_STRINGS | CHOIR_STRINGS | GROUP_STRINGS

#: Annotation labels for specialist roles (cea_* annotation defaults).
ROLE_ANNOTATIONS: dict[str, str] = {
    "arranger": "arr.",
    "instrument arranger": "arr.",
    "vocal arranger": "arr.",
    "orchestrator": "orch.",
    "reconstructed by": "reconstructed",
    "revised by": "revised",
    "translator": "trans.",
    "lyricist": "lyrics",
    "librettist": "libretto",
    "writer": "writer",
    "chorus master": "choirmaster",
    "concertmaster": "leader",
    "balance": "balance",
    "producer": "producer",
}

#: Relationship types that map to the ARRANGER tag (CE convention).
ARRANGER_RELS: frozenset[str] = frozenset(
    {"arranger", "instrument arranger", "vocal arranger", "orchestrator", "reconstructed by", "revised by"}
)

#: Classical Extras default period map: (name, start_year_inclusive, end_year_inclusive).
PERIOD_MAP: list[tuple[str, int, int]] = [
    ("Early", -3000, 800),
    ("Medieval", 800, 1400),
    ("Renaissance", 1400, 1600),
    ("Baroque", 1600, 1750),
    ("Classical", 1750, 1820),
    ("Early Romantic", 1800, 1850),
    ("Late Romantic", 1850, 1910),
    ("20th Century", 1910, 1975),
    ("Contemporary", 1975, 2525),
]

#: Mapping from MB work type to genre string (CE worktype_genres logic).
WORKTYPE_GENRES: dict[str, str] = {
    "Symphony": "Symphony",
    "Concerto": "Concerto",
    "Opera": "Opera",
    "Oratorio": "Oratorio",
    "Cantata": "Cantata",
    "Mass": "Mass",
    "Motet": "Motet",
    "Ballet": "Ballet",
    "Symphonic poem": "Symphonic poem",
    "Suite": "Suite",
    "Overture": "Overture",
    "Chamber music": "Chamber music",
    "Sonata": "Sonata",
    "Song cycle": "Song-cycle",
    "Choral": "Choral",
    "Partita": "Partita",
    "Aria": "Aria",
}

#: Supported audio file extensions.
AUDIO_EXTENSIONS: frozenset[str] = frozenset({".flac", ".mp3", ".ogg", ".m4a", ".aac", ".wav"})

#: Regex matching filesystem-unsafe characters for safe_name().
_SAFE_RE: re.Pattern[str] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: In-process cache: work_id → MBWork, avoids redundant API calls for shared parents.
_WORK_CACHE: dict[str, MBWork] = {}

_P = ParamSpec("_P")
_T = TypeVar("_T")

# ---------------------------------------------------------------------------
# MusicBrainz helpers
# ---------------------------------------------------------------------------


def init_mb(user_agent: str) -> None:
    """Configure the musicbrainzngs user-agent from a ``"App/Version contact"`` string.

    Args:
        user_agent: A user-agent string of the form ``"AppName/1.0 contact@example.com"``.
            The part before the first ``/`` is the application name; the first token
            after it is the version; the remainder is the contact string.
    """
    parts = user_agent.split("/", 1)
    app = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else "1.0"
    vc = rest.split(None, 1)
    version = vc[0]
    contact = vc[1] if len(vc) > 1 else ""
    mb.set_useragent(app, version, contact)


def _mb_retry(fn: Callable[_P, _T]) -> Callable[_P, _T]:
    """Decorator: exponential-backoff retry on MB rate-limit errors.

    Wraps any callable that may raise :class:`musicbrainzngs.ResponseError`
    with a six-attempt retry loop, sleeping ``2 ** attempt`` seconds between
    retries on 429, 503, or 500 responses.

    Args:
        fn: The callable to wrap.

    Returns:
        A wrapped version of ``fn`` with the same signature.

    Raises:
        mb.ResponseError: If the error is not a rate-limit / server error.
        RuntimeError: If all six retry attempts are exhausted.
    """

    @functools.wraps(fn)
    def _wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        for attempt in range(6):
            try:
                return fn(*args, **kwargs)
            except mb.ResponseError as exc:
                code = str(exc)
                if any(s in code for s in ("503", "429", "500")):
                    wait = 2**attempt
                    log.warning("mb_rate_limit", code=code[:20], wait_s=wait, attempt=attempt)
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"MB request failed after retries: {fn.__name__}")

    return _wrapper


@_mb_retry
def _get_release_by_id(release_id: str) -> dict[str, JSON]:
    """Thin typed wrapper around ``mb.get_release_by_id`` for ``@_mb_retry``.

    Args:
        release_id: The MusicBrainz release MBID.

    Returns:
        The raw response dict from ``musicbrainzngs``.
    """
    result: dict[str, JSON] = mb.get_release_by_id(
        release_id,
        includes=[
            "artists",
            "recordings",
            "release-groups",
            "labels",
            "media",
            "artist-credits",
            "work-rels",
            "recording-level-rels",
        ],
    )
    return result


def fetch_release(release_id: str) -> MBRelease:
    """Fetch a full MusicBrainz release with all includes needed for annotation.

    Args:
        release_id: The MusicBrainz release MBID (UUID string).

    Returns:
        An :class:`~music_annotator.models.MBRelease` instance populated from
        the ``musicbrainzngs`` response.

    Raises:
        mb.ResponseError: On a non-retryable API error.
        RuntimeError: If all retry attempts are exhausted.
    """
    log.info("fetch_release", release_id=release_id)
    result = _get_release_by_id(release_id)
    time.sleep(1)
    return MBRelease.model_validate(result.get("release", {}))


@_mb_retry
def _get_recording_by_id(recording_id: str) -> dict[str, JSON]:
    """Thin typed wrapper around ``mb.get_recording_by_id`` for ``@_mb_retry``.

    Args:
        recording_id: The MusicBrainz recording MBID.

    Returns:
        The raw response dict from ``musicbrainzngs``.
    """
    result: dict[str, JSON] = mb.get_recording_by_id(
        recording_id,
        includes=["artists", "work-rels", "artist-rels"],
    )
    return result


def fetch_recording_detail(recording_id: str) -> MBRecording:
    """Fetch a recording with its artist and work relationships.

    Args:
        recording_id: The MusicBrainz recording MBID.

    Returns:
        An :class:`~music_annotator.models.MBRecording` instance populated from
        the ``musicbrainzngs`` response.

    Raises:
        mb.ResponseError: On a non-retryable API error.
        RuntimeError: If all retry attempts are exhausted.
    """
    log.debug("fetch_recording", recording_id=recording_id)
    result = _get_recording_by_id(recording_id)
    time.sleep(1)
    return MBRecording.model_validate(result.get("recording", {}))


def fetch_cover_art(release_id: str, release_group_id: str = "") -> CoverArt:
    """Download the front cover art for a release from the Cover Art Archive.

    Strategy:

    1. Try the release's own CAA entry via ``mb.get_image_front()``.
    2. On HTTP 404, try the release-group front via
       ``mb.get_release_group_image_front()``.
    3. On any remaining error, return an empty :class:`~music_annotator.models.CoverArt`.

    The MIME type is inferred from image magic bytes:
    ``\\xff\\xd8`` → ``image/jpeg``; ``\\x89PNG`` → ``image/png``.

    Args:
        release_id: The MusicBrainz release MBID.
        release_group_id: The MusicBrainz release-group MBID (used as fallback).
            Pass an empty string to skip the fallback.

    Returns:
        A :class:`~music_annotator.models.CoverArt` instance whose ``data`` is
        non-empty on success, or whose ``data`` is ``b""`` when no art was found.

    Raises:
        Nothing — all ``mb.ResponseError`` exceptions are caught and logged.
    """

    def _infer_mime(data: bytes) -> str:
        if data[:2] == b"\xff\xd8":
            return "image/jpeg"
        if data[:4] == b"\x89PNG":
            return "image/png"
        return "image/jpeg"

    log.info("fetch_cover_art", release_id=release_id)
    try:
        raw = mb.get_image_front(release_id, size="500")
        time.sleep(1)
        if raw:
            data = bytes(raw)
            return CoverArt(data=data, mime=_infer_mime(data))
    except mb.ResponseError as exc:
        code = str(exc)
        match code:
            case s if "404" in s:
                log.info("cover_art_no_release_entry", release_id=release_id)
            case _:
                log.warning("cover_art_release_error", code=code[:40])

    if release_group_id:
        try:
            raw = mb.get_release_group_image_front(release_group_id, size="500")
            time.sleep(1)
            if raw:
                data = bytes(raw)
                return CoverArt(data=data, mime=_infer_mime(data))
        except mb.ResponseError as exc:
            log.warning("cover_art_release_group_error", code=str(exc)[:40])

    log.warning("cover_art_unavailable", release_id=release_id)
    return CoverArt()


@_mb_retry
def _get_work_by_id(work_id: str) -> dict[str, JSON]:
    """Thin typed wrapper around ``mb.get_work_by_id`` for ``@_mb_retry``.

    Args:
        work_id: The MusicBrainz work MBID.

    Returns:
        The raw response dict from ``musicbrainzngs``.
    """
    result: dict[str, JSON] = mb.get_work_by_id(
        work_id,
        includes=["artist-rels", "work-rels", "url-rels", "tags", "aliases"],
    )
    return result


def fetch_work_detail(work_id: str) -> MBWork:
    """Fetch a work with artist relationships, parent work links, tags and aliases.

    Results are cached in the module-level ``_WORK_CACHE`` dict so that shared
    parent works (e.g. a symphonic poem that is the parent of four movements) are
    only fetched once per process.

    Args:
        work_id: The MusicBrainz work MBID.

    Returns:
        An :class:`~music_annotator.models.MBWork` instance populated from the
        ``musicbrainzngs`` response.

    Raises:
        mb.ResponseError: On a non-retryable API error.
        RuntimeError: If all retry attempts are exhausted.
    """
    if work_id in _WORK_CACHE:
        log.debug("fetch_work_cache_hit", work_id=work_id)
        return _WORK_CACHE[work_id]
    log.debug("fetch_work", work_id=work_id)
    result = _get_work_by_id(work_id)
    time.sleep(1)
    work = MBWork.model_validate(result.get("work", {}))
    _WORK_CACHE[work_id] = work
    return work


# ---------------------------------------------------------------------------
# Artist / performer classification helpers (CE-style)
# ---------------------------------------------------------------------------


def is_ensemble(name: str) -> bool:
    """Return True if the artist name contains an ensemble-identifying substring.

    Args:
        name: The artist display name.

    Returns:
        ``True`` when any token from :data:`ENSEMBLE_STRINGS` appears in the
        lowercased name.
    """
    low = name.lower()
    return any(s in low for s in ENSEMBLE_STRINGS)


def is_choir(name: str) -> bool:
    """Return True if the artist name contains a choir-identifying substring.

    Args:
        name: The artist display name.

    Returns:
        ``True`` when any token from :data:`CHOIR_STRINGS` appears in the
        lowercased name.
    """
    low = name.lower()
    return any(s in low for s in CHOIR_STRINGS)


def is_orchestra(name: str) -> bool:
    """Return True if the artist name contains an orchestra-identifying substring.

    Args:
        name: The artist display name.

    Returns:
        ``True`` when any token from :data:`ORCHESTRA_STRINGS` appears in the
        lowercased name.
    """
    low = name.lower()
    return any(s in low for s in ORCHESTRA_STRINGS)


def artist_credit_phrase(credit_list: list[MBArtistCredit | str]) -> str:
    """Reconstruct the display credit phrase from a MusicBrainz artist-credit list.

    The MB API returns artist-credit as a list mixing :class:`~music_annotator.models.MBArtistCredit`
    instances (for actual artists) and plain strings (for join phrases like ``" & "``).

    Args:
        credit_list: The ``artist-credit`` list from a MB response.

    Returns:
        The concatenated display string, e.g. ``"Karajan & Berliner Philharmoniker"``.
    """
    parts: list[str] = []
    for item in credit_list:
        match item:
            case str():
                parts.append(item)
            case MBArtistCredit():
                parts.append(item.name or item.artist.name)
            case _:  # pragma: no cover
                pass
    return "".join(parts)


def artist_ids(credit_list: list[MBArtistCredit | str]) -> list[str]:
    """Extract MBIDs from an artist-credit list.

    Args:
        credit_list: The ``artist-credit`` list from a MB response.

    Returns:
        A list of MBID strings for all :class:`~music_annotator.models.MBArtistCredit`
        entries in the credit list, in order.
    """
    return [item.artist.id for item in credit_list if isinstance(item, MBArtistCredit) and item.artist.id]


def artist_sort_names(credit_list: list[MBArtistCredit | str]) -> list[str]:
    """Extract sort-names from an artist-credit list.

    Entries with no artist MBID (i.e., join-phrase-only dicts) are skipped.

    Args:
        credit_list: The ``artist-credit`` list from a MB response.

    Returns:
        A list of sort-name strings (falling back to display name when absent)
        for all :class:`~music_annotator.models.MBArtistCredit` entries in the
        credit list that have a non-empty artist MBID, in order.
    """
    result_names: list[str] = []
    for item in credit_list:
        if isinstance(item, MBArtistCredit) and item.artist.id:
            result_names.append(item.artist.sort_name or item.artist.name)
    return result_names


def last_name(sort_name: str) -> str:
    """Extract the last name from a MusicBrainz sort-name ``"Surname, Forename"``.

    Args:
        sort_name: A sort-name string, typically ``"Surname, Forename"`` or just
            a single name.

    Returns:
        The part of the sort-name before the first comma, stripped of whitespace.
        Returns the full string if no comma is present.
    """
    return sort_name.split(",")[0].strip()


# ---------------------------------------------------------------------------
# Work hierarchy builder (CE _cwp_ convention)
# ---------------------------------------------------------------------------


def build_work_hierarchy(work: MBWork, visited: set[str] | None = None) -> list[MBWork]:
    """Walk up the MusicBrainz work parent chain and return the hierarchy list.

    The returned list runs from the recording's direct (bottom) work at index 0
    to the root (top) work at the highest index, matching the Classical Extras
    ``_cwp_work_0`` … ``_cwp_work_N`` convention.

    Args:
        work: The bottom-level :class:`~music_annotator.models.MBWork` as
            returned by :func:`fetch_work_detail`.
        visited: Set of work MBIDs already visited in this traversal (used to
            prevent infinite loops on circular parent references).  Pass
            ``None`` to start a fresh traversal.

    Returns:
        A list of :class:`~music_annotator.models.MBWork` instances from
        bottom (index 0) to top (last index).

    Raises:
        mb.ResponseError: If fetching a parent work fails with a non-retryable error.
        RuntimeError: If all retry attempts for a parent work fetch are exhausted.
    """
    if visited is None:
        visited = set()
    if work.id in visited:
        return [work]
    visited.add(work.id)

    hierarchy: list[MBWork] = [work]

    for rel in work.work_relation_list:
        if rel.direction == "backward" and rel.type in ("parts", "part of"):
            parent_id = rel.work.id
            if parent_id and parent_id not in visited:
                log.debug("fetch_parent_work", parent_id=parent_id, child_id=work.id)
                parent = fetch_work_detail(parent_id)
                hierarchy.extend(build_work_hierarchy(parent, visited))
                break  # take first parent only

    return hierarchy


def strip_common_prefix(child: str, parent: str) -> str:
    """Remove from ``child`` any text that duplicates ``parent``, producing a short label.

    Implements the CE ``_cwp_part_n`` stripping logic:

    1. If ``child`` starts with ``parent`` (case-insensitive), strip that prefix
       and any leading punctuation/whitespace.
    2. Otherwise, if ``child`` contains a colon, return the portion after the
       first colon.
    3. Otherwise return ``child`` unchanged.

    Args:
        child: The full work name at the lower hierarchy level (e.g.
            ``"Fontane di Roma, P 106: I. La fontana di Valle Giulia all'alba"``).
        parent: The work name of the parent level (e.g. ``"Fontane di Roma, P 106"``).

    Returns:
        The stripped part label (e.g. ``"I. La fontana di Valle Giulia all'alba"``),
        or ``child`` unchanged when no prefix is found.
    """
    if not parent or not child:
        return child
    if child.lower().startswith(parent.lower()):
        stripped = child[len(parent) :].lstrip(" :.-–—,")
        return stripped if stripped else child
    if ":" in child:
        after_colon = child.split(":", 1)[1].strip()
        return after_colon if after_colon else child
    return child


def period_for_year(year: int | None) -> str:
    """Map a composition year to the corresponding Classical Extras period name.

    Args:
        year: The four-digit composition year, or ``None`` when unknown.

    Returns:
        A period name from :data:`PERIOD_MAP` (e.g. ``"Late Romantic"``), or an
        empty string when ``year`` is ``None`` or falls outside all defined ranges.
    """
    if year is None:
        return ""
    for name, start, end in PERIOD_MAP:
        if start <= year <= end:
            return name
    return ""


# ---------------------------------------------------------------------------
# Work-level data extraction helpers
# ---------------------------------------------------------------------------


def extract_work_artist_rels(work: MBWork, role_buckets: RoleBuckets) -> None:
    """Fill ``role_buckets`` from the work's ``artist-relation-list``.

    Follows the CE ``_cwp_`` convention.  Deduplicates entries by MBID so
    that the same composer credited at every level of the work hierarchy
    (movement → symphonic poem → collection) appears only once.

    Args:
        work: The :class:`~music_annotator.models.MBWork` instance.
        role_buckets: The :class:`~music_annotator.models.RoleBuckets` instance
            to populate in-place.

    Returns:
        None.  ``role_buckets`` is mutated directly.
    """
    for rel in work.artist_relation_list:
        entry = ArtistEntry(name=rel.artist.name, sort=rel.artist.sort_name or rel.artist.name, mbid=rel.artist.id)
        match rel.type:
            case "composer" | "writer":
                role_buckets.add_unique("composers", entry)
            case "lyricist":
                role_buckets.add_unique("lyricists", entry)
            case "librettist":
                role_buckets.add_unique("librettists", entry)
            case "translator":
                role_buckets.add_unique("translators", entry)
            case "arranger" | "instrument arranger" | "vocal arranger":
                role_buckets.add_unique("arrangers", entry)
            case "orchestrator":
                role_buckets.add_unique("orchestrators", entry)
            case "reconstructed by":
                role_buckets.add_unique("reconstructors", entry)
            case "revised by":
                role_buckets.add_unique("revisors", entry)


def collect_work_dates(work: MBWork) -> WorkDates:
    """Extract composed, published, and premiered dates from a work's attributes.

    Checks both the ``attribute-list`` (for typed date attributes) and the
    ``life-span.begin`` field (used as a composed-date fallback by MB).

    Args:
        work: The :class:`~music_annotator.models.MBWork` instance.

    Returns:
        A :class:`~music_annotator.models.WorkDates` instance with any dates found.
        Fields default to empty strings when not present.
    """
    composed = published = premiered = ""
    for attr in work.attribute_list:
        if not isinstance(attr, MBAttribute):
            continue
        val = attr.value
        t = attr.type.lower()
        match t:
            case s if "composed" in s or "composition" in s:
                composed = val
            case s if "published" in s or "publish" in s:
                published = val
            case s if "premiered" in s or "premiere" in s:
                premiered = val
    if not composed and work.life_span.begin:
        composed = work.life_span.begin[:4]
    return WorkDates(composed=composed, published=published, premiered=premiered)


def parse_year(date_str: str) -> int | None:
    """Parse the first four-digit year from a date string.

    Args:
        date_str: A date string such as ``"1900"``, ``"1900-06-15"``, or ``""``.

    Returns:
        The integer year, or ``None`` when no four-digit sequence is found or
        the input is empty.
    """
    if not date_str:
        return None
    m = re.match(r"(\d{4})", date_str)
    return int(m.group(1)) if m else None


def collect_work_tags_and_key(work: MBWork) -> tuple[list[str], str]:
    """Return the folksonomy tag list and key signature from a work.

    Args:
        work: The :class:`~music_annotator.models.MBWork` instance.

    Returns:
        A 2-tuple ``(tag_names, key_signature)`` where ``tag_names`` is a list
        of folksonomy tag name strings and ``key_signature`` is the key
        signature string (e.g. ``"G minor"``), or ``""`` when absent.
    """
    tags: list[str] = [t.name for t in work.tag_list]
    key: str = work.key
    for attr in work.attribute_list:
        if isinstance(attr, MBAttribute) and attr.type.lower() in ("key", "key signature"):
            key = attr.value
    return tags, key


# ---------------------------------------------------------------------------
# CEA and CWP builders
# ---------------------------------------------------------------------------


def build_cea_performers(recording_detail: MBRecording) -> CeaPerformers:
    """Classify recording-level artist relations into CE ``_cea_*`` performer buckets.

    Args:
        recording_detail: The :class:`~music_annotator.models.MBRecording`
            instance as returned by :func:`fetch_recording_detail`.

    Returns:
        A populated :class:`~music_annotator.models.CeaPerformers` instance.
    """
    cea = CeaPerformers()
    for rel in recording_detail.artist_relation_list:
        name = rel.artist.name
        sort = rel.artist.sort_name or name
        mid = rel.artist.id
        entry = ArtistEntry(name=name, sort=sort, mbid=mid)

        match rel.type:
            case "conductor":
                cea.conductors.append(entry)
            case "chorus master":
                cea.chorusmasters.append(entry)
            case "concertmaster":
                cea.leaders.append(entry)
            case "arranger" | "instrument arranger" | "vocal arranger":
                cea.arrangers.append(entry)
            case "orchestrator":
                cea.orchestrators.append(entry)
            case "composer" | "writer":
                cea.composers.append(entry)
            case "producer":
                cea.producers.append(entry)
            case "balance":
                cea.engineers.append(entry)
            case "performer" | "instrument" | "vocal" | "performing orchestra":
                if is_ensemble(name):
                    cea.ensembles.append(entry)
                else:
                    first_attr = rel.attribute_list[0] if rel.attribute_list else ""
                    instr: str = first_attr.value if isinstance(first_attr, MBAttribute) else first_attr
                    entry = ArtistEntry(name=name, sort=sort, mbid=mid, instrument=instr)
                    vocal_keywords = ("soprano", "mezzo", "tenor", "baritone", "bass", "contralto", "voice", "vocal", "singer")
                    if any(v in instr.lower() for v in vocal_keywords):
                        cea.vocalists.append(entry)
                    elif instr:
                        cea.instrumentalists.append(entry)
                    else:
                        cea.other_soloists.append(entry)

    return cea


def build_cwp_tags(
    work_hierarchy: list[MBWork],
    role_buckets: RoleBuckets,
) -> CwpTags:
    """Build Classical Extras ``_cwp_*`` tag values from the resolved work hierarchy.

    Args:
        work_hierarchy: The list of :class:`~music_annotator.models.MBWork`
            instances from bottom (index 0) to top (last index), as returned
            by :func:`build_work_hierarchy`.
        role_buckets: The :class:`~music_annotator.models.RoleBuckets` already
            populated by :func:`extract_work_artist_rels` for every level.

    Returns:
        A :class:`~music_annotator.models.CwpTags` instance with all
        ``cwp_*`` fields populated.
    """
    cwp = CwpTags()
    if not work_hierarchy:
        return cwp

    n_levels = len(work_hierarchy)
    cwp.part_levels = n_levels - 1

    # Build per-level name/id maps before stripping
    work_names: dict[int, str] = {}
    work_ids: dict[int, str] = {}
    for i, w in enumerate(work_hierarchy):
        work_names[i] = w.title
        work_ids[i] = w.id

    top_work = work_hierarchy[-1]
    cwp.work_top = top_work.title
    cwp.workid_top = top_work.id

    # Dates and key from bottom-level work
    dates = collect_work_dates(work_hierarchy[0])
    cwp.composed_dates = dates.composed
    cwp.published_dates = dates.published
    cwp.premiered_dates = dates.premiered
    _, key = collect_work_tags_and_key(work_hierarchy[0])
    cwp.keys = key
    cwp.worktype_genres = work_hierarchy[0].type

    # Strip part names
    part_names: dict[int, str] = {}
    for i in range(n_levels):
        parent_name = work_names.get(i + 1, "") if i < n_levels - 1 else ""
        part_names[i] = strip_common_prefix(work_names[i], parent_name)

    # Assemble levels list
    cwp.levels = [
        WorkHierarchyLevel(
            index=i,
            work_id=work_ids[i],
            work_title=work_names[i],
            part_title=part_names[i],
        )
        for i in range(n_levels)
    ]

    if n_levels == 1:
        cwp.work = work_names[0]
        cwp.groupheading = work_names[0]
        cwp.part = ""
    else:
        cwp.work = cwp.work_top
        gh_parts = [cwp.work_top]
        for j in range(n_levels - 2, 0, -1):
            inter_part = part_names.get(j, work_names.get(j, ""))
            if inter_part:
                gh_parts.append(inter_part)
        bottom_part = part_names.get(0, "")
        if bottom_part:
            gh_parts.append(bottom_part)
        cwp.groupheading = " :: ".join(gh_parts)
        cwp.part = bottom_part

    if n_levels > 2:
        inter_parts = [part_names.get(j, work_names.get(j, "")) for j in range(1, n_levels - 1)]
        cwp.inter_work = " :: ".join(p for p in inter_parts if p)

    # Period
    year = parse_year(cwp.composed_dates)
    cwp.period = period_for_year(year)

    # Work-level artist roles
    if role_buckets.composers:
        cwp.composers = "; ".join(e.name for e in role_buckets.composers)
        cwp.composers_sort = "; ".join(e.sort for e in role_buckets.composers)
        cwp.composer_lastnames = "; ".join(last_name(e.sort) for e in role_buckets.composers)
    for role_name in ("arrangers", "orchestrators", "reconstructors", "revisors", "lyricists", "librettists", "translators"):
        bucket: list[ArtistEntry] = getattr(role_buckets, role_name)
        if bucket:
            setattr(cwp, role_name, "; ".join(e.name for e in bucket))
            setattr(cwp, f"{role_name}_sort", "; ".join(e.sort for e in bucket))

    return cwp


# ---------------------------------------------------------------------------
# Main metadata builder
# ---------------------------------------------------------------------------


def build_track_tags(
    release: MBRelease,
    track: MBTrack,
    medium_pos: int,
    recording_detail: MBRecording,
    work_hierarchy: list[MBWork],
) -> TrackTags:
    """Build the complete tag model for one track, implementing all CE conventions.

    This is the central function that combines release, recording, and work
    hierarchy data into a :class:`~music_annotator.models.TrackTags` instance
    ready for writing to an audio file.

    Args:
        release: The :class:`~music_annotator.models.MBRelease` from :func:`fetch_release`.
        track: The :class:`~music_annotator.models.MBTrack` for this track.
        medium_pos: The 1-based disc/medium position (typically ``1`` for single-disc releases).
        recording_detail: The :class:`~music_annotator.models.MBRecording` from
            :func:`fetch_recording_detail`.
        work_hierarchy: The work hierarchy list from :func:`build_work_hierarchy`,
            or an empty list when no work link was found.

    Returns:
        A :class:`~music_annotator.models.TrackTags` instance with all fields populated.
        Movement number and total fields (``movementnumber``, ``movementtotal``,
        ``cwp_movt_num``, ``cwp_movt_tot``, ``cwp_single_work_album``) are left
        empty strings at this stage; they are filled in by the caller after all
        tracks have been processed.
    """
    rec = track.recording

    # Release-level artists
    album_artist_phrase = artist_credit_phrase(release.artist_credit)
    album_artist_ids_str = "/".join(artist_ids(release.artist_credit))
    album_artist_sort = "; ".join(artist_sort_names(release.artist_credit))

    # Recording artist credit (from the recording stub on the track)
    rec_artist_phrase = artist_credit_phrase(rec.artist_credit)
    rec_artist_ids_str = "/".join(artist_ids(rec.artist_credit))
    rec_artist_sort = "; ".join(artist_sort_names(rec.artist_credit))

    # Label / catalogue
    label_info = release.label_info_list[0] if release.label_info_list else None
    label_name = label_info.label.name if label_info else ""
    catalog_number = label_info.catalog_number if label_info else ""

    # Track counts
    medium = next((m for m in release.medium_list if m.position == medium_pos), None)
    total_tracks = str(len(medium.track_list) if medium else 0)

    # CEA classification
    cea = build_cea_performers(recording_detail)
    all_soloists = cea.all_soloists

    # Work hierarchy + roles
    role_buckets = RoleBuckets()
    for w in work_hierarchy:
        extract_work_artist_rels(w, role_buckets)
    cwp = build_cwp_tags(work_hierarchy, role_buckets)

    # Direct work link from recording
    direct_work_id = ""
    direct_work_title = ""
    for rel in recording_detail.work_relation_list:
        if rel.type == "performance":
            direct_work_id = rel.work.id
            direct_work_title = rel.work.title
            break

    # Derive COMPOSER
    composer_name = composer_sort = composer_id = ""
    if role_buckets.composers:
        composer_name = "; ".join(e.name for e in role_buckets.composers)
        composer_sort = "; ".join(e.sort for e in role_buckets.composers)
        composer_id = "/".join(e.mbid for e in role_buckets.composers)
    elif cea.composers:
        composer_name = "; ".join(e.name for e in cea.composers)
        composer_sort = "; ".join(e.sort for e in cea.composers)
        composer_id = "/".join(e.mbid for e in cea.composers)

    conductor_name = "; ".join(e.name for e in cea.conductors)
    conductor_id = "/".join(e.mbid for e in cea.conductors)
    chorusmaster = "; ".join(e.name for e in cea.chorusmasters)
    leader = "; ".join(e.name for e in cea.leaders)

    # Arranger string (annotated with role in parens per CE convention)
    arranger_seen: set[str] = set()
    arranger_parts: list[str] = []
    for e in cea.arrangers:
        arranger_parts.append(e.name)
        arranger_seen.add(e.name)
    for e in role_buckets.arrangers:
        if e.name not in arranger_seen:
            arranger_parts.append(e.name)
            arranger_seen.add(e.name)
    for e in role_buckets.orchestrators:
        if e.name not in arranger_seen:
            arranger_parts.append(f"{e.name} (orch.)")
            arranger_seen.add(e.name)
    for e in role_buckets.reconstructors:
        arranger_parts.append(f"{e.name} (reconstructed)")
    for e in role_buckets.revisors:
        arranger_parts.append(f"{e.name} (revised)")
    arranger_str = "; ".join(arranger_parts)

    lyricist_str = "; ".join(e.name for e in role_buckets.lyricists + role_buckets.librettists)
    translator_str = "; ".join(e.name for e in role_buckets.translators)

    # Performer strings
    soloist_names = [e.name for e in all_soloists]
    soloist_str = "; ".join(f"{e.name} ({e.instrument})" if e.instrument else e.name for e in all_soloists)
    ensemble_names = [e.name for e in cea.ensembles]
    ensemble_str = "; ".join(ensemble_names)
    vocalist_str = "; ".join(f"{e.name} ({e.instrument})" if e.instrument else e.name for e in cea.vocalists)
    instrumentalist_str = "; ".join(f"{e.name} ({e.instrument})" if e.instrument else e.name for e in cea.instrumentalists)
    instruments_str = "; ".join(e.instrument for e in all_soloists if e.instrument)

    recording_artist_names = [e.name for e in all_soloists + cea.ensembles]
    if cea.conductors:
        recording_artist_names += [e.name for e in cea.conductors]
    cea_recording_artist = "; ".join(recording_artist_names) or rec_artist_phrase

    # Final work/movement tags
    _level0_title = cwp.levels[0].work_id and cwp.levels[0].work_title if cwp.levels else ""
    work_tag = cwp.work_top or _level0_title or direct_work_title or ""
    groupheading = cwp.groupheading or work_tag
    part_tag = cwp.part or (cwp.levels[0].part_title if cwp.levels else "")
    wtype_genre = WORKTYPE_GENRES.get(cwp.worktype_genres, "")
    genre = wtype_genre or "Classical"

    tags = TrackTags(
        cea_conductors_list=cea.conductors,
        cea_ensembles_list=cea.ensembles,
        title=rec.title,
        artist=rec_artist_phrase,
        artists=rec_artist_phrase,
        artistsort=rec_artist_sort,
        albumartist=album_artist_phrase,
        albumartistsort=album_artist_sort,
        album=release.title,
        tracknumber=str(track.position),
        totaltracks=total_tracks,
        discnumber=str(medium_pos),
        date=release.date,
        originaldate=release.release_group.first_release_date,
        media="CD",
        script=release.text_representation.script,
        language=release.text_representation.language,
        releasetype=release.release_group.primary_type,
        releasestatus=release.status,
        organization=label_name,
        label=label_name,
        catalognumber=catalog_number,
        barcode=release.barcode,
        work=work_tag,
        groupheading=groupheading,
        top_work=cwp.work_top or work_tag,
        part=part_tag,
        movement=part_tag,
        subtitle=part_tag,
        composer=composer_name,
        composersort=composer_sort,
        conductor=conductor_name,
        lyricist=lyricist_str,
        translator=translator_str,
        arranger=arranger_str,
        chorusmaster=chorusmaster,
        leader=leader,
        soloists=soloist_str,
        ensemble=ensemble_str,
        band=ensemble_str,
        vocalists=vocalist_str,
        instrumentalists=instrumentalist_str,
        instrument=instruments_str,
        genre=genre,
        period=cwp.period,
        key=cwp.keys,
        is_classical="1",
        work_year=cwp.composed_dates or cwp.published_dates or cwp.premiered_dates,
        composed_date=cwp.composed_dates,
        published_date=cwp.published_dates,
        premiered_date=cwp.premiered_dates,
        producer="; ".join(e.name for e in cea.producers),
        engineer="; ".join(e.name for e in cea.engineers),
        musicbrainz_albumid=release.id,
        musicbrainz_trackid=track.id,
        musicbrainz_recordingid=rec.id,
        musicbrainz_releasegroupid=release.release_group.id,
        musicbrainz_albumartistid=album_artist_ids_str,
        musicbrainz_artistid=rec_artist_ids_str,
        musicbrainz_workid=direct_work_id or (cwp.levels[0].work_id if cwp.levels else ""),
        musicbrainz_conductorid=conductor_id,
        musicbrainz_composerid=composer_id,
        musicbrainz_releasetrackid=track.id,
        cea_recording_artist=cea_recording_artist,
        cea_soloists=soloist_str,
        cea_soloist_names="; ".join(soloist_names),
        cea_vocalists=vocalist_str,
        cea_instrumentalists=instrumentalist_str,
        cea_other_soloists="; ".join(e.name for e in cea.other_soloists),
        cea_ensembles=ensemble_str,
        cea_ensemble_names="; ".join(ensemble_names),
        cea_conductors=conductor_name,
        cea_composers=cwp.composers or composer_name,
        cea_composer_lastnames=cwp.composer_lastnames or last_name(composer_sort),
        cea_performers=rec_artist_phrase,
        cea_arrangers=arranger_str,
        cea_orchestrators="; ".join(e.name for e in role_buckets.orchestrators),
        cea_chorusmasters=chorusmaster,
        cea_leaders=leader,
        cea_instruments=instruments_str,
        cwp_work_top=cwp.work_top,
        cwp_workid_top=cwp.workid_top,
        cwp_part_levels=str(cwp.part_levels),
        cwp_part=cwp.part,
        cwp_work=cwp.work,
        cwp_groupheading=cwp.groupheading,
        cwp_inter_work=cwp.inter_work,
        cwp_composers=cwp.composers,
        cwp_composers_sort=cwp.composers_sort,
        cwp_composer_lastnames=cwp.composer_lastnames,
        cwp_arrangers=cwp.arrangers,
        cwp_arrangers_sort=cwp.arrangers_sort,
        cwp_orchestrators=cwp.orchestrators,
        cwp_orchestrators_sort=cwp.orchestrators_sort,
        cwp_lyricists=cwp.lyricists,
        cwp_lyricists_sort=cwp.lyricists_sort,
        cwp_librettists=cwp.librettists,
        cwp_librettists_sort=cwp.librettists_sort,
        cwp_translators=cwp.translators,
        cwp_translators_sort=cwp.translators_sort,
        cwp_keys=cwp.keys,
        cwp_composed_dates=cwp.composed_dates,
        cwp_published_dates=cwp.published_dates,
        cwp_premiered_dates=cwp.premiered_dates,
        cwp_worktype_genres=cwp.worktype_genres,
    )

    # Add per-level fields as model_extra
    for level in cwp.levels:
        i = level.index
        tags.model_extra[f"cwp_work_{i}"] = level.work_title  # type: ignore[index]
        tags.model_extra[f"cwp_workid_{i}"] = level.work_id  # type: ignore[index]
        tags.model_extra[f"cwp_part_{i}"] = level.part_title  # type: ignore[index]

    return tags


# ---------------------------------------------------------------------------
# Directory / filename helpers
# ---------------------------------------------------------------------------


def safe_name(s: str, max_len: int = 80) -> str:
    """Sanitise a string for use as a filesystem path component.

    Replaces characters forbidden on common filesystems (Windows/POSIX) with
    underscores, strips leading/trailing dots and spaces, and truncates to
    ``max_len`` characters.

    Args:
        s: The raw name string.
        max_len: Maximum length of the returned string.  Defaults to 80.

    Returns:
        A sanitised string safe for use as a directory or file name.
    """
    s = _SAFE_RE.sub("_", s).strip(". ")
    return s[:max_len]


def _rec_title(track: MBTrack) -> str:
    """Return the recording title for a track.

    Args:
        track: An :class:`~music_annotator.models.MBTrack` instance.

    Returns:
        The title of the nested recording, or ``"Unknown"`` when absent.
    """
    return track.recording.title or "Unknown"


def build_dest_path(dest_root: Path, release: MBRelease, track: MBTrack, tags: TrackTags) -> Path:
    """Compute the destination path (without extension) for one annotated track.

    Layout::

        <dest_root>/
          <Composer last names> - <Conductor; Ensemble>/
            <Work title> (<work MBID>)/
              <nn> - <movement title>

    The numeric prefix is the movement number within the work (not the album
    track number).  Width is 2 digits normally; 3 digits when the work
    contains more than 99 movements.

    Args:
        dest_root: The root destination directory.
        release: The :class:`~music_annotator.models.MBRelease` from :func:`fetch_release`.
        track: The :class:`~music_annotator.models.MBTrack` for this track.
        tags: The :class:`~music_annotator.models.TrackTags` instance for this
            track, which must already have ``movementnumber`` and
            ``movementtotal`` filled in.

    Returns:
        A :class:`~pathlib.Path` for the destination file *without* extension
        (callers append ``.flac``, ``.mp3``, etc.).
    """
    file_dict = tags.to_file_dict()

    # Composer directory component
    raw_composer = file_dict.get("CWP_COMPOSER_LASTNAMES") or file_dict.get("CEA_COMPOSER_LASTNAMES", "")
    if raw_composer:
        seen: set[str] = set()
        unique: list[str] = []
        for part in raw_composer.split("; "):
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                unique.append(part)
        composer = "; ".join(unique)
    else:
        composer = ""
        for credit in release.artist_credit:
            if isinstance(credit, MBArtistCredit) and credit.artist.type == "Person":
                composer = credit.artist.sort_name or credit.artist.name
                break
        if not composer:
            composer = "Unknown Composer"

    # Performers directory component
    conductors = [e.name for e in tags.cea_conductors_list]
    ensembles = [e.name for e in tags.cea_ensembles_list]
    if conductors or ensembles:
        performers = "; ".join(conductors + ensembles)
    else:
        performers = file_dict.get("CEA_ENSEMBLE_NAMES") or file_dict.get("ARTIST", "Unknown Performers")

    # Work directory component
    work_title = file_dict.get("CWP_WORK_TOP") or file_dict.get("WORK", "")
    work_mbid = file_dict.get("CWP_WORKID_TOP") or file_dict.get("MUSICBRAINZ_WORKID", "")
    work_dir = safe_name(work_title)
    if work_mbid:
        work_dir = f"{work_dir} ({work_mbid})"

    # Movement number prefix
    movt_num = int(file_dict.get("MOVEMENTNUMBER") or str(track.position))
    movt_tot = int(file_dict.get("MOVEMENTTOTAL") or "1")
    width = 3 if movt_tot > 99 else 2
    track_num = str(movt_num).zfill(width)

    top_dir = safe_name(f"{composer} - {performers}")
    track_title = safe_name(file_dict.get("TITLE") or _rec_title(track))

    return dest_root / top_dir / work_dir / f"{track_num} - {track_title}"


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------


def apply_tags_flac(dest_file: Path, tags: TrackTags, cover: CoverArt | None = None) -> None:
    """Write Vorbis Comment tags and optional cover art to a FLAC file.

    Clears any existing tags, writes all non-internal non-empty fields from
    ``tags``, and (if provided) embeds ``cover`` as a front-cover PICTURE block.

    Args:
        dest_file: Path to the destination FLAC file (must already exist).
        tags: The :class:`~music_annotator.models.TrackTags` instance to write.
        cover: Optional :class:`~music_annotator.models.CoverArt`; when
            ``cover.available`` is ``True`` the image is embedded as a
            type-3 (front cover) PICTURE block.

    Returns:
        None.

    Raises:
        mutagen.MutagenError: If the file cannot be read or written.
    """
    audio = FLAC(str(dest_file))
    audio.clear()
    for key, value in tags.to_file_dict().items():
        audio[key.lower()] = value
    if cover and cover.available:
        pic = FLACPicture()
        pic.type = 3  # front cover
        pic.mime = cover.mime or "image/jpeg"
        pic.desc = "Cover"
        pic.width = pic.height = pic.depth = pic.colors = 0
        pic.data = cover.data
        audio.add_picture(pic)
    audio.save()
    log.debug("tagged_flac", path=str(dest_file))


def apply_tags_mp3(dest_file: Path, tags: TrackTags, cover: CoverArt | None = None) -> None:
    """Write ID3v2.4 tags and optional cover art to an MP3 file.

    Deletes any existing ID3 tags, writes standard text frames and TXXX frames
    for all non-internal non-empty fields, and (if provided) adds an APIC frame.

    Args:
        dest_file: Path to the destination MP3 file (must already exist).
        tags: The :class:`~music_annotator.models.TrackTags` instance to write.
        cover: Optional :class:`~music_annotator.models.CoverArt`; when
            ``cover.available`` is ``True`` the image is embedded as APIC
            type 3 (front cover).

    Returns:
        None.

    Raises:
        mutagen.MutagenError: If the file cannot be read or written.
    """
    try:
        audio = MP3(str(dest_file))
        if audio.tags:
            audio.tags.delete(str(dest_file))
    except Exception:  # noqa: BLE001
        pass

    id3_tags = ID3()
    file_dict = tags.to_file_dict()

    def txxx(desc: str, val: str) -> None:
        if val:
            id3_tags.add(TXXX(encoding=3, desc=desc, text=val))

    if file_dict.get("TITLE"):
        id3_tags.add(TIT2(encoding=3, text=file_dict["TITLE"]))
    if file_dict.get("ARTIST"):
        id3_tags.add(TPE1(encoding=3, text=file_dict["ARTIST"]))
    if file_dict.get("ALBUMARTIST"):
        id3_tags.add(TPE2(encoding=3, text=file_dict["ALBUMARTIST"]))
    if file_dict.get("ALBUM"):
        id3_tags.add(TALB(encoding=3, text=file_dict["ALBUM"]))
    if file_dict.get("TRACKNUMBER"):
        total = file_dict.get("TOTALTRACKS", "")
        trck_text = f"{file_dict['TRACKNUMBER']}/{total}" if total else file_dict["TRACKNUMBER"]
        id3_tags.add(TRCK(encoding=3, text=trck_text))
    if file_dict.get("DISCNUMBER"):
        id3_tags.add(TPOS(encoding=3, text=file_dict["DISCNUMBER"]))
    if file_dict.get("DATE"):
        id3_tags.add(TDRC(encoding=3, text=file_dict["DATE"]))
    if file_dict.get("ORIGINALDATE"):
        id3_tags.add(TDOR(encoding=3, text=file_dict["ORIGINALDATE"]))
    if file_dict.get("COMPOSER"):
        id3_tags.add(TCOM(encoding=3, text=file_dict["COMPOSER"]))
    if file_dict.get("CONDUCTOR"):
        id3_tags.add(TPE3(encoding=3, text=file_dict["CONDUCTOR"]))
    if file_dict.get("ORGANIZATION"):
        id3_tags.add(TPUB(encoding=3, text=file_dict["ORGANIZATION"]))

    txxx_map: dict[str, str] = {
        "MUSICBRAINZ_ALBUMID": "MusicBrainz Album Id",
        "MUSICBRAINZ_TRACKID": "MusicBrainz Release Track Id",
        "MUSICBRAINZ_RECORDINGID": "MusicBrainz Track Id",
        "MUSICBRAINZ_RELEASEGROUPID": "MusicBrainz Release Group Id",
        "MUSICBRAINZ_ALBUMARTISTID": "MusicBrainz Album Artist Id",
        "MUSICBRAINZ_ARTISTID": "MusicBrainz Artist Id",
        "MUSICBRAINZ_WORKID": "MusicBrainz Work Id",
        "MUSICBRAINZ_CONDUCTORID": "MusicBrainz Conductor Id",
        "MUSICBRAINZ_COMPOSERID": "MusicBrainz Composer Id",
        "CATALOGNUMBER": "CATALOGNUMBER",
        "BARCODE": "BARCODE",
        "WORK": "WORK",
        "GROUPHEADING": "GROUPHEADING",
        "TOP_WORK": "TOP_WORK",
        "PART": "PART",
        "MOVEMENT": "MOVEMENT",
        "MOVEMENTNUMBER": "MOVEMENTNUMBER",
        "MOVEMENTTOTAL": "MOVEMENTTOTAL",
        "IS_CLASSICAL": "IS_CLASSICAL",
        "GENRE": "GENRE",
        "PERIOD": "PERIOD",
        "KEY": "KEY",
        "WORK_YEAR": "WORK_YEAR",
        "COMPOSED_DATE": "COMPOSED_DATE",
        "LANGUAGE": "LANGUAGE",
        "SCRIPT": "SCRIPT",
        "RELEASETYPE": "MusicBrainz Album Type",
        "RELEASESTATUS": "MusicBrainz Album Status",
        "SOLOISTS": "SOLOISTS",
        "ENSEMBLE": "ENSEMBLE",
        "CEA_RECORDING_ARTIST": "CEA_RECORDING_ARTIST",
        "CEA_SOLOISTS": "CEA_SOLOISTS",
        "CEA_ENSEMBLES": "CEA_ENSEMBLES",
        "CEA_CONDUCTORS": "CEA_CONDUCTORS",
        "CEA_COMPOSERS": "CEA_COMPOSERS",
        "CWP_WORK_TOP": "CWP_WORK_TOP",
        "CWP_GROUPHEADING": "CWP_GROUPHEADING",
        "CWP_PART": "CWP_PART",
        "CWP_COMPOSERS": "CWP_COMPOSERS",
        "CWP_KEYS": "CWP_KEYS",
        "CWP_COMPOSED_DATES": "CWP_COMPOSED_DATES",
        "CWP_WORKTYPE_GENRES": "CWP_WORKTYPE_GENRES",
    }
    for meta_key, txxx_desc in txxx_map.items():
        txxx(txxx_desc, file_dict.get(meta_key, ""))

    if cover and cover.available:
        id3_tags.add(
            APIC(
                encoding=3,
                mime=cover.mime or "image/jpeg",
                type=3,
                desc="Cover",
                data=cover.data,
            )
        )

    id3_tags.save(str(dest_file), v2_version=4)
    log.debug("tagged_mp3", path=str(dest_file))


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def find_source_files(src_dir: Path) -> list[Path]:
    """Return sorted list of audio files in ``src_dir``.

    Args:
        src_dir: Directory to scan.  Only the immediate children are checked
            (not recursive).

    Returns:
        A list of :class:`~pathlib.Path` objects for all files whose extension
        (lowercased) is in :data:`AUDIO_EXTENSIONS`, sorted by filename.

    Raises:
        OSError: If ``src_dir`` does not exist or is not readable.
    """
    return sorted(
        (p for p in src_dir.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda p: p.name,
    )


def run(
    release_id: str,
    src_dir: Path,
    dest_root: Path,
    user_agent: str,
    dry_run: bool = False,
    fetch_rels: bool = True,
) -> None:
    """Copy and tag an album directory using MusicBrainz metadata.

    This is the top-level entry point for the annotation pipeline:

    1. Initialise the MB user-agent and fetch the release.
    2. Fetch cover art from the Cover Art Archive.
    3. For each track (paired with a source file by position):

       a. Fetch the recording's artist and work relationships.
       b. Walk up the work hierarchy to build ``cwp_work_N`` levels.
       c. Build the full :class:`~music_annotator.models.TrackTags` model.

    4. Compute movement numbers and totals grouped by top-work MBID.
    5. Copy each source file to the destination tree and apply tags.
    6. Restore the source file's atime/mtime on the destination.

    Args:
        release_id: The MusicBrainz release MBID.
        src_dir: Directory containing the source audio files.  Files are
            matched to release tracks by sorted filename order.
        dest_root: Root directory of the destination music library.
        user_agent: User-agent string passed to :func:`init_mb`.
        dry_run: When ``True``, log planned operations without copying or
            writing any files.
        fetch_rels: When ``False``, skip per-recording lookups and produce
            minimal tags (faster but incomplete).

    Returns:
        None.

    Raises:
        mb.ResponseError: On a non-retryable MusicBrainz API error.
        RuntimeError: If all retry attempts are exhausted for any API call.
        OSError: If source files cannot be read or destination files cannot be
            written.
    """
    init_mb(user_agent)

    log.info("fetch_release_start", release_id=release_id)
    release = fetch_release(release_id)
    log.info("fetch_release_done", title=release.title, date=release.date)

    # Flatten all tracks to (MBTrack, medium_pos) pairs
    all_track_pairs: list[tuple[MBTrack, int]] = []
    for medium in release.medium_list:
        for track in medium.track_list:
            all_track_pairs.append((track, medium.position))

    # Fetch cover art once for the whole release
    rg_id = release.release_group.id
    cover = CoverArt()
    if not dry_run:
        cover = fetch_cover_art(release_id, rg_id)
        if cover.available:
            log.info("cover_art_fetched", size=len(cover.data), mime=cover.mime)
        else:
            log.warning("cover_art_not_available", release_id=release_id)

    src_files = find_source_files(src_dir)
    log.info("source_files", count=len(src_files))
    log.info("release_tracks", count=len(all_track_pairs))

    if len(src_files) != len(all_track_pairs):
        log.warning(
            "track_count_mismatch",
            src_files=len(src_files),
            release_tracks=len(all_track_pairs),
        )

    # Pair each source file with its (MBTrack, medium_pos)
    file_track_pairs = list(zip(src_files, all_track_pairs))

    # tags_map: (src_file, track, medium_pos) → TrackTags
    tags_map: dict[int, TrackTags] = {}

    if fetch_rels and not dry_run:
        log.info("fetch_recording_rels_start")
        for idx, (src_file, (track, medium_pos)) in enumerate(file_track_pairs):
            rec_id = track.recording.id
            log.info("fetch_recording", position=track.position, title=track.recording.title[:60])

            rec_detail = fetch_recording_detail(rec_id)

            work_hierarchy: list[MBWork] = []
            for rel in rec_detail.work_relation_list:
                if rel.type == "performance":
                    bottom_work_id = rel.work.id
                    if bottom_work_id:
                        log.debug("fetch_bottom_work", work_id=bottom_work_id)
                        bottom_work = fetch_work_detail(bottom_work_id)
                        work_hierarchy = build_work_hierarchy(bottom_work)
                    break

            tags_map[idx] = build_track_tags(release, track, medium_pos, rec_detail, work_hierarchy)

        # Compute movement numbers grouped by top work MBID
        top_work_groups: dict[str, list[int]] = defaultdict(list)
        for idx, (_, (track, _medium_pos)) in enumerate(file_track_pairs):
            t = tags_map[idx]
            twid = t.cwp_workid_top or t.musicbrainz_workid
            top_work_groups[twid].append(idx)

        for _twid, group_idxs in top_work_groups.items():
            total = len(group_idxs)
            single = len(top_work_groups) == 1
            for movt_idx, grp_idx in enumerate(group_idxs, start=1):
                tags_obj = tags_map[grp_idx]
                tags_obj.movementnumber = str(movt_idx)
                tags_obj.movementtotal = str(total)
                tags_obj.cwp_movt_num = str(movt_idx)
                tags_obj.cwp_movt_tot = str(total)
                tags_obj.cwp_single_work_album = "1" if single else "0"

    else:
        label_info = release.label_info_list[0] if release.label_info_list else None
        for idx, (src_file, (track, _medium_pos)) in enumerate(file_track_pairs):
            tags_map[idx] = TrackTags(
                title=track.recording.title,
                artist=artist_credit_phrase(track.recording.artist_credit),
                albumartist=artist_credit_phrase(release.artist_credit),
                album=release.title,
                tracknumber=str(track.position),
                date=release.date,
                musicbrainz_albumid=release.id,
                musicbrainz_recordingid=track.recording.id,
                musicbrainz_trackid=track.id,
                releasetype=release.release_group.primary_type,
                label=label_info.label.name if label_info else "",
                catalognumber=label_info.catalog_number if label_info else "",
                barcode=release.barcode,
            )

    # Copy and tag
    for idx, (src_file, (track, _medium_pos)) in enumerate(file_track_pairs):
        final_tags = tags_map[idx]
        dest_base = build_dest_path(dest_root, release, track, final_tags)
        dest_file = dest_base.with_suffix(src_file.suffix.lower())

        log.info("copy_track", src=src_file.name, dest=str(dest_file.relative_to(dest_root)))

        if dry_run:
            log.info(
                "dry_run_track",
                composer=final_tags.composer,
                conductor=final_tags.conductor,
                work=final_tags.work,
                period=final_tags.period,
            )
            continue

        dest_file.parent.mkdir(parents=True, exist_ok=True)

        # Capture source timestamps before copying; mutagen's .save() bumps mtime.
        # On Linux, ctime (inode-change time) cannot be set by userspace.
        src_stat = src_file.stat()
        src_times = (src_stat.st_atime, src_stat.st_mtime)

        shutil.copy2(src_file, dest_file)

        ext = src_file.suffix.lower()
        try:
            match ext:
                case ".flac":
                    apply_tags_flac(dest_file, final_tags, cover)
                case ".mp3":
                    apply_tags_mp3(dest_file, final_tags, cover)
                case _:
                    log.warning("unsupported_format", ext=ext, file=dest_file.name)
        except Exception as exc:  # noqa: BLE001
            log.error("tag_error", file=dest_file.name, error=str(exc))

        os.utime(dest_file, src_times)

    log.info("run_complete", dest=str(dest_root))
