"""music-annotator — Copy and tag a classical music album using MusicBrainz metadata.

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

import datetime
import functools
import hashlib
import json
import os
import re
import shutil
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ParamSpec, TypeVar

import musicbrainzngs as mb
import structlog
import yaml
from mutagen.flac import FLAC
from mutagen.flac import Picture as FLACPicture
from mutagen.id3 import (  # type: ignore[attr-defined]
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
from rich.console import Console

from music_annotator.models import (
    JSON,
    ArtistEntry,
    CeaPerformers,
    CoverArt,
    CoverImage,
    CwpTags,
    MBArtistCredit,
    MBAttribute,
    MBRecording,
    MBRelease,
    MBReleaseCandidate,
    MBTrack,
    MBWork,
    RoleBuckets,
    TrackTags,
    TransactionEntry,
    TransactionLog,
    WorkDates,
    WorkHierarchyLevel,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)
_console: Console = Console()


def configure_color(enabled: bool) -> None:
    """Replace the module-level rich :class:`~rich.console.Console` instance to enable or disable color output.

    Called by :func:`~music_annotator.__main__._configure_logging` when the ``--no-color`` CLI flag is present.

    :param enabled: When ``False``, replace the console with one that produces plain text without ANSI codes.
    """
    global _console  # noqa: PLW0603  # pylint: disable=global-statement
    _console = Console(no_color=not enabled)


__all__ = [
    "configure_color",
    "init_mb",
    "fetch_release",
    "fetch_recording_detail",
    "fetch_cover_art",
    "CoverImage",
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
    "_sha256_file",
    "_read_tags_flac",
    "_read_tags_mp3",
    "_verify_copy",
    "run",
    "parse_disc_info_yaml",
    "parse_disc_toc",
    "parse_dir_hint",
    "search_releases_by_dir",
    "discover",
    "write_transaction_log",
    "JOURNAL_FILENAME",
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

    Splits the string on the first ``/`` to extract the application name, then splits the remainder on the first whitespace to
    separate version from contact.  The extracted values are passed directly to ``mb.set_useragent``.

    :param user_agent: A user-agent string of the form ``"AppName/1.0 contact@example.com"``.
    """
    parts = user_agent.split("/", 1)
    app = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else "1.0"
    vc = rest.split(None, 1)
    version = vc[0]
    contact = vc[1] if len(vc) > 1 else ""
    mb.set_useragent(app, version, contact)


def _mb_retry(fn: Callable[_P, _T]) -> Callable[_P, _T]:
    """Decorator that wraps a callable with exponential-backoff retry on MB rate-limit errors.

    Attempts the call up to six times, sleeping ``2 ** attempt`` seconds between retries when the response error contains
    ``"429"``, ``"503"``, or ``"500"``.  Any other :class:`~musicbrainzngs.ResponseError` is re-raised immediately.

    :param fn: The callable to wrap.
    :returns: A wrapped version of ``fn`` with the same signature.
    :raises mb.ResponseError: If the error is not a rate-limit or server error.
    :raises RuntimeError: If all six retry attempts are exhausted.
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
    """Thin typed wrapper around ``mb.get_release_by_id`` decorated with ``@_mb_retry``.

    Requests all includes needed for full annotation: artists, recordings, release groups, labels, media, artist credits,
    work relations, and recording-level relations.

    :param release_id: The MusicBrainz release MBID.
    :returns: The raw response dict from ``musicbrainzngs``.
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

    Calls :func:`_get_release_by_id` (which is retried on rate-limit errors), waits one second as a polite delay, then
    validates the ``"release"`` key of the response into an :class:`~music_annotator.models.MBRelease` model.

    :param release_id: The MusicBrainz release MBID (UUID string).
    :returns: An :class:`~music_annotator.models.MBRelease` instance populated from the ``musicbrainzngs`` response.
    :raises mb.ResponseError: On a non-retryable API error.
    :raises RuntimeError: If all retry attempts are exhausted.
    """
    log.info("fetch_release", release_id=release_id)
    result = _get_release_by_id(release_id)
    time.sleep(1)
    return MBRelease.model_validate(result.get("release", {}))


@_mb_retry
def _get_recording_by_id(recording_id: str) -> dict[str, JSON]:
    """Thin typed wrapper around ``mb.get_recording_by_id`` decorated with ``@_mb_retry``.

    Requests artist credits, work relations, and artist relations.

    :param recording_id: The MusicBrainz recording MBID.
    :returns: The raw response dict from ``musicbrainzngs``.
    """
    result: dict[str, JSON] = mb.get_recording_by_id(
        recording_id,
        includes=["artists", "work-rels", "artist-rels", "work-level-rels"],
    )
    return result


def fetch_recording_detail(recording_id: str) -> MBRecording:
    """Fetch a recording with its artist and work relationships.

    Calls :func:`_get_recording_by_id` (retried on rate-limit errors), waits one second, then validates the
    ``"recording"`` key into an :class:`~music_annotator.models.MBRecording` model.

    :param recording_id: The MusicBrainz recording MBID.
    :returns: An :class:`~music_annotator.models.MBRecording` instance populated from the ``musicbrainzngs`` response.
    :raises mb.ResponseError: On a non-retryable API error.
    :raises RuntimeError: If all retry attempts are exhausted.
    """
    log.debug("fetch_recording", recording_id=recording_id)
    result = _get_recording_by_id(recording_id)
    time.sleep(1)
    return MBRecording.model_validate(result.get("recording", {}))


def fetch_cover_art(release_id: str, release_group_id: str = "") -> CoverArt:
    """Download all available cover art for a release from the Cover Art Archive.

    Strategy:

    1. Call ``mb.get_image_list(release_id)`` to obtain the full CAA image listing for the release.
    2. For each image entry in the listing, classify it by its ``types`` list into one of: ``front``
       (``"Front"``), ``back`` (``"Back"``), ``booklet`` (``"Booklet"``), or ``medium`` (``"Medium"``).
       Images whose types do not include any of these four strings are skipped.
    3. Fetch the binary data for each classified image via ``mb.get_image(release_id, coverid, size="500")``,
       sleeping 1 second after each network call to respect the 1 req/s rate limit.
    4. If the release has no CAA listing (HTTP 404) and ``release_group_id`` is provided, fall back to
       fetching the release-group front image via ``mb.get_release_group_image_front()`` and place it in
       ``front`` only.

    The MIME type for each image is inferred from magic bytes: ``\\xff\\xd8`` → ``image/jpeg``;
    ``\\x89PNG`` → ``image/png``; anything else defaults to ``image/jpeg``.

    :param release_id: The MusicBrainz release MBID.
    :param release_group_id: The MusicBrainz release-group MBID used as a fallback when the release has no
        CAA listing.  Pass an empty string to skip the fallback.
    :returns: A :class:`~music_annotator.models.CoverArt` instance populated with all retrieved images, or
        an empty :class:`~music_annotator.models.CoverArt` when nothing could be fetched.
    """

    def _infer_mime(data: bytes) -> str:
        if data[:2] == b"\xff\xd8":
            return "image/jpeg"
        if data[:4] == b"\x89PNG":
            return "image/png"
        return "image/jpeg"

    def _fetch_image(rel_id: str, coverid: str | int) -> CoverImage | None:
        """Fetch a single image by cover ID; return a CoverImage or None on error."""
        try:
            raw = mb.get_image(rel_id, coverid, size="500")
            time.sleep(1)
            if raw:
                data = bytes(raw)
                return CoverImage(data=data, mime=_infer_mime(data))
        except mb.ResponseError as exc:
            log.warning("cover_art_image_error", coverid=str(coverid), code=str(exc)[:40])
        return None

    log.info("fetch_cover_art", release_id=release_id)

    # Attempt to get the full image listing for this release.
    listing: list[dict[str, JSON]] = []
    has_release_listing = False
    try:
        result = mb.get_image_list(release_id)
        images = result.get("images", [])
        if isinstance(images, list):
            listing = [img for img in images if isinstance(img, dict)]
        has_release_listing = True
    except mb.ResponseError as exc:
        code = str(exc)
        match code:
            case s if "404" in s:
                log.info("cover_art_no_release_listing", release_id=release_id)
            case _:
                log.warning("cover_art_listing_error", code=code[:40])

    front: list[CoverImage] = []
    back: list[CoverImage] = []
    booklet: list[CoverImage] = []
    medium: list[CoverImage] = []

    if has_release_listing:
        for img in listing:
            types_raw = img.get("types", [])
            if not isinstance(types_raw, list):
                continue
            types = [t for t in types_raw if isinstance(t, str)]
            coverid = img.get("id", "")
            if not coverid:
                continue
            match types:
                case t if "Front" in t:
                    image = _fetch_image(release_id, str(coverid))
                    if image:
                        front.append(image)
                case t if "Back" in t:
                    image = _fetch_image(release_id, str(coverid))
                    if image:
                        back.append(image)
                case t if "Booklet" in t:
                    image = _fetch_image(release_id, str(coverid))
                    if image:
                        booklet.append(image)
                case t if "Medium" in t:
                    image = _fetch_image(release_id, str(coverid))
                    if image:
                        medium.append(image)
                case _:
                    log.debug("cover_art_skipped_type", types=types, coverid=str(coverid))

    # If no release listing was available, fall back to the release-group front image.
    if not has_release_listing and release_group_id:
        try:
            raw = mb.get_release_group_image_front(release_group_id, size="500")
            time.sleep(1)
            if raw:
                data = bytes(raw)
                front.append(CoverImage(data=data, mime=_infer_mime(data)))
        except mb.ResponseError as exc:
            log.warning("cover_art_release_group_error", code=str(exc)[:40])

    result_art = CoverArt(front=front, back=back, booklet=booklet, medium=medium)
    if result_art.available:
        log.info(
            "cover_art_fetched",
            front=len(front),
            back=len(back),
            booklet=len(booklet),
            medium=len(medium),
        )
    else:
        log.warning("cover_art_unavailable", release_id=release_id)
    return result_art


@_mb_retry
def _get_work_by_id(work_id: str) -> dict[str, JSON]:
    """Thin typed wrapper around ``mb.get_work_by_id`` decorated with ``@_mb_retry``.

    Requests artist relations, work relations, URL relations, tags, and aliases.

    :param work_id: The MusicBrainz work MBID.
    :returns: The raw response dict from ``musicbrainzngs``.
    """
    result: dict[str, JSON] = mb.get_work_by_id(
        work_id,
        includes=["artist-rels", "work-rels", "url-rels", "tags", "aliases"],
    )
    return result


def fetch_work_detail(work_id: str) -> MBWork:
    """Fetch a work with artist relationships, parent work links, tags, and aliases.

    Results are cached in the module-level :data:`_WORK_CACHE` dict so that shared parent works (e.g. a symphonic poem
    that is the parent of four movements) are only fetched once per process.

    :param work_id: The MusicBrainz work MBID.
    :returns: An :class:`~music_annotator.models.MBWork` instance populated from the ``musicbrainzngs`` response.
    :raises mb.ResponseError: On a non-retryable API error.
    :raises RuntimeError: If all retry attempts are exhausted.
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


def _get_bottom_work(embedded: MBWork) -> MBWork:
    """Return the bottom work for a performance relation, using inlined data when available.

    When ``musicbrainzngs`` is called with the ``work-level-rels`` include, the MB API inlines the
    full work detail (including its own ``artist-relation-list`` and ``work-relation-list``) directly
    inside the recording response.  In that case ``embedded`` already carries all the data needed and
    no extra network round-trip is required.

    If ``embedded`` has empty relation lists (stub shape — ``work-level-rels`` was absent or the
    library did not parse the inlined data), fall back to :func:`fetch_work_detail`.

    :param embedded: The :class:`~music_annotator.models.MBWork` extracted from the recording's
        performance ``work-relation-list`` entry.
    :returns: A fully populated :class:`~music_annotator.models.MBWork`.
    """
    if embedded.artist_relation_list or embedded.work_relation_list:
        log.debug("bottom_work_inlined", work_id=embedded.id)
        return embedded
    log.debug("fetch_bottom_work", work_id=embedded.id)
    return fetch_work_detail(embedded.id)


# ---------------------------------------------------------------------------
# Artist / performer classification helpers (CE-style)
# ---------------------------------------------------------------------------


def is_ensemble(name: str) -> bool:
    """Return ``True`` if the artist name contains an ensemble-identifying substring.

    Checks against the union set :data:`ENSEMBLE_STRINGS` which covers orchestras, choirs, and chamber groups.

    :param name: The artist display name.
    :returns: ``True`` when any token from :data:`ENSEMBLE_STRINGS` appears in the lowercased name.
    """
    low = name.lower()
    return any(s in low for s in ENSEMBLE_STRINGS)


def is_choir(name: str) -> bool:
    """Return ``True`` if the artist name contains a choir-identifying substring.

    :param name: The artist display name.
    :returns: ``True`` when any token from :data:`CHOIR_STRINGS` appears in the lowercased name.
    """
    low = name.lower()
    return any(s in low for s in CHOIR_STRINGS)


def is_orchestra(name: str) -> bool:
    """Return ``True`` if the artist name contains an orchestra-identifying substring.

    :param name: The artist display name.
    :returns: ``True`` when any token from :data:`ORCHESTRA_STRINGS` appears in the lowercased name.
    """
    low = name.lower()
    return any(s in low for s in ORCHESTRA_STRINGS)


def artist_credit_phrase(credit_list: list[MBArtistCredit | str]) -> str:
    """Reconstruct the display credit phrase from a MusicBrainz ``artist-credit`` list.

    The MB API returns ``artist-credit`` as a mixed list of :class:`~music_annotator.models.MBArtistCredit` instances
    (for actual artists) and plain strings (for join phrases like ``" & "``).  This function concatenates the credited name
    of each artist (falling back to the artist's canonical name) with any intervening join-phrase strings.

    :param credit_list: The ``artist-credit`` list from a MB response.
    :returns: The concatenated display string, e.g. ``"Karajan & Berliner Philharmoniker"``.
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
    """Extract MBIDs from an ``artist-credit`` list, skipping plain join-phrase strings.

    :param credit_list: The ``artist-credit`` list from a MB response.
    :returns: A list of MBID strings for all :class:`~music_annotator.models.MBArtistCredit` entries that have a
        non-empty ``artist.id``, in order.
    """
    return [item.artist.id for item in credit_list if isinstance(item, MBArtistCredit) and item.artist.id]


def artist_sort_names(credit_list: list[MBArtistCredit | str]) -> list[str]:
    """Extract sort-names from an ``artist-credit`` list, skipping join-phrase-only entries.

    Entries with no artist MBID (i.e. join-phrase-only dicts such as ``{"joinphrase": " & "}``) are skipped because they
    do not represent a real credited artist.

    :param credit_list: The ``artist-credit`` list from a MB response.
    :returns: A list of sort-name strings (falling back to the display name when ``sort_name`` is absent) for all
        :class:`~music_annotator.models.MBArtistCredit` entries that have a non-empty ``artist.id``, in order.
    """
    result_names: list[str] = []
    for item in credit_list:
        if isinstance(item, MBArtistCredit) and item.artist.id:
            result_names.append(item.artist.sort_name or item.artist.name)
    return result_names


def last_name(sort_name: str) -> str:
    """Extract the last name from a MusicBrainz sort-name of the form ``"Surname, Forename"``.

    :param sort_name: A sort-name string, typically ``"Surname, Forename"`` or just a single token.
    :returns: The part of the sort-name before the first comma, stripped of whitespace.  Returns the full string when no
        comma is present.
    """
    return sort_name.split(",")[0].strip()


# ---------------------------------------------------------------------------
# Work hierarchy builder (CE _cwp_ convention)
# ---------------------------------------------------------------------------


def build_work_hierarchy(work: MBWork, visited: set[str] | None = None) -> list[MBWork]:
    """Walk up the MusicBrainz work parent chain and return the full hierarchy list.

    The returned list runs from the recording's direct (bottom) work at index 0 to the root (top) work at the highest
    index, matching the Classical Extras ``cwp_work_0`` … ``cwp_work_N`` tag convention.  Only the first backward
    ``"parts"`` or ``"part of"`` relation at each level is followed; cycle detection via ``visited`` prevents infinite
    loops on circular parent references.

    :param work: The bottom-level :class:`~music_annotator.models.MBWork` as returned by :func:`fetch_work_detail`.
    :param visited: Set of work MBIDs already visited in this traversal.  Pass ``None`` to start a fresh traversal.
    :returns: A list of :class:`~music_annotator.models.MBWork` instances from bottom (index 0) to top (last index).
    :raises mb.ResponseError: If fetching a parent work fails with a non-retryable error.
    :raises RuntimeError: If all retry attempts for a parent work fetch are exhausted.
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
    """Remove from ``child`` any text that duplicates ``parent``, producing a short movement label.

    Implements the CE ``cwp_part_N`` stripping logic:

    1. If ``child`` starts with ``parent`` (case-insensitive), strip that prefix and any leading punctuation or whitespace.
    2. Otherwise, if ``child`` contains a colon, return the portion after the first colon.
    3. Otherwise return ``child`` unchanged.

    :param child: The full work name at the lower hierarchy level
        (e.g. ``"Fontane di Roma, P 106: I. La fontana di Valle Giulia all'alba"``).
    :param parent: The work name at the parent level (e.g. ``"Fontane di Roma, P 106"``).
    :returns: The stripped part label (e.g. ``"I. La fontana di Valle Giulia all'alba"``),
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

    Iterates :data:`PERIOD_MAP` and returns the name of the first entry whose inclusive ``[start, end]`` range contains
    ``year``.

    :param year: The four-digit composition year, or ``None`` when unknown.
    :returns: A period name from :data:`PERIOD_MAP` (e.g. ``"Late Romantic"``), or an empty string when ``year`` is
        ``None`` or falls outside all defined ranges.
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
    """Fill ``role_buckets`` in-place from the work's ``artist-relation-list``.

    Follows the CE ``cwp_`` convention.  Deduplicates entries by MBID so that the same composer credited at every level
    of the work hierarchy (movement → symphonic poem → collection) appears only once.  Unknown relation types are silently
    ignored.

    :param work: The :class:`~music_annotator.models.MBWork` instance.
    :param role_buckets: The :class:`~music_annotator.models.RoleBuckets` instance to populate in-place.
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

    Checks both the ``attribute-list`` (for typed date attributes) and the ``life-span.begin`` field (used as a
    composed-date fallback by MB when no explicit composition-date attribute is present).

    :param work: The :class:`~music_annotator.models.MBWork` instance.
    :returns: A :class:`~music_annotator.models.WorkDates` instance with any dates found.  Fields default to empty
        strings when not present.
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

    :param date_str: A date string such as ``"1900"``, ``"1900-06-15"``, or ``""``.
    :returns: The integer year, or ``None`` when no four-digit sequence is found or the input is empty.
    """
    if not date_str:
        return None
    m = re.match(r"(\d{4})", date_str)
    return int(m.group(1)) if m else None


def collect_work_tags_and_key(work: MBWork) -> tuple[list[str], str]:
    """Return the folksonomy tag list and key signature from a work.

    The key is taken from :attr:`~music_annotator.models.MBWork.key` and overridden by any ``attribute-list`` entry
    whose ``type`` is ``"key"`` or ``"key signature"``.

    :param work: The :class:`~music_annotator.models.MBWork` instance.
    :returns: A 2-tuple ``(tag_names, key_signature)`` where ``tag_names`` is a list of folksonomy tag name strings and
        ``key_signature`` is the key signature string (e.g. ``"G minor"``), or ``""`` when absent.
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
    """Classify recording-level artist relations into CE ``cea_*`` performer buckets.

    Iterates the ``artist-relation-list`` of the recording and routes each entry into the appropriate bucket of the
    returned :class:`~music_annotator.models.CeaPerformers` instance.  For ``"performer"``-type relations the first
    ``attribute-list`` entry is used as the instrument label; entries matching :func:`is_ensemble` go to ``ensembles``;
    entries with a vocal keyword in the instrument label go to ``vocalists``; all others go to ``instrumentalists`` (with
    an instrument label) or ``other_soloists`` (without).

    :param recording_detail: The :class:`~music_annotator.models.MBRecording` instance as returned by
        :func:`fetch_recording_detail`.
    :returns: A populated :class:`~music_annotator.models.CeaPerformers` instance.
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
    """Build Classical Extras ``cwp_*`` tag values from the resolved work hierarchy.

    Constructs the full :class:`~music_annotator.models.CwpTags` model by:

    - Setting ``work_top``/``workid_top`` from the root work.
    - Collecting dates, key, and work-type genre from the bottom work.
    - Stripping common name prefixes to produce per-level ``part_title`` values.
    - Building ``groupheading`` from the top work title and all intermediate part titles.
    - Mapping composed date to a CE period name via :func:`period_for_year`.
    - Populating all artist role strings from ``role_buckets``.

    :param work_hierarchy: List of :class:`~music_annotator.models.MBWork` from bottom (index 0) to top (last index), as
        returned by :func:`build_work_hierarchy`.
    :param role_buckets: A :class:`~music_annotator.models.RoleBuckets` already populated by
        :func:`extract_work_artist_rels` for every level of the hierarchy.
    :returns: A :class:`~music_annotator.models.CwpTags` instance with all ``cwp_*`` fields populated.
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

    This is the central function that combines release, recording, and work-hierarchy data into a
    :class:`~music_annotator.models.TrackTags` instance ready for writing to an audio file.  The movement-number fields
    (``movementnumber``, ``movementtotal``, ``cwp_movt_num``, ``cwp_movt_tot``, ``cwp_single_work_album``) are left as
    empty strings at this stage; they are filled in by :func:`run` after all tracks have been processed and grouped by
    top-work MBID.

    :param release: The :class:`~music_annotator.models.MBRelease` from :func:`fetch_release`.
    :param track: The :class:`~music_annotator.models.MBTrack` for this track.
    :param medium_pos: The 1-based disc/medium position (typically ``1`` for single-disc releases).
    :param recording_detail: The :class:`~music_annotator.models.MBRecording` from :func:`fetch_recording_detail`.
    :param work_hierarchy: The work hierarchy list from :func:`build_work_hierarchy`, or an empty list when no work link
        was found.
    :returns: A :class:`~music_annotator.models.TrackTags` instance with all fields populated except movement-number
        fields.
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

    Replaces characters forbidden on common filesystems (Windows/POSIX) with underscores, strips leading and trailing
    dots and spaces, and truncates to ``max_len`` characters.

    :param s: The raw name string.
    :param max_len: Maximum length of the returned string.  Defaults to ``80``.
    :returns: A sanitised string safe for use as a directory or file name.
    """
    s = _SAFE_RE.sub("_", s).strip(". ")
    return s[:max_len]


def _rec_title(track: MBTrack) -> str:
    """Return the recording title for a track, falling back to ``"Unknown"``.

    :param track: An :class:`~music_annotator.models.MBTrack` instance.
    :returns: The title of the nested recording, or ``"Unknown"`` when absent.
    """
    return track.recording.title or "Unknown"


def build_dest_path(dest_root: Path, release: MBRelease, track: MBTrack, tags: TrackTags) -> Path:
    """Compute the destination path (without extension) for one annotated track.

    Layout::

        <dest_root>/
          <Composer last names> - <Conductor; Ensemble>/
            <Work title> (<work MBID>)/
              <nn> - <movement title>

    The numeric prefix is the movement number within the work (not the album track number).  Width is 2 digits normally;
    3 digits when the work contains more than 99 movements.

    :param dest_root: The root destination directory.
    :param release: The :class:`~music_annotator.models.MBRelease` from :func:`fetch_release`.
    :param track: The :class:`~music_annotator.models.MBTrack` for this track.
    :param tags: The :class:`~music_annotator.models.TrackTags` instance for this track, which must already have
        ``movementnumber`` and ``movementtotal`` filled in.
    :returns: A :class:`~pathlib.Path` for the destination file *without* extension (callers append ``.flac``, ``.mp3``,
        etc.).
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
    """Write Vorbis Comment tags and all available cover art pictures to a FLAC file.

    Clears any existing tags and PICTURE blocks, writes all non-internal non-empty fields from ``tags`` as lowercase
    Vorbis Comment keys, then embeds every image in ``cover`` as a FLAC PICTURE block using the appropriate
    ``PictureType`` value for each CAA image category:

    - ``front`` images → ``PictureType.COVER_FRONT`` (3)
    - ``back`` images → ``PictureType.COVER_BACK`` (4)
    - ``booklet`` images → ``PictureType.LEAFLET_PAGE`` (5)
    - ``medium`` images → ``PictureType.MEDIA`` (6)

    All images within a category are embedded in listing order.  When multiple images share the same ``PictureType``,
    each gets a unique ``desc`` suffixed with its 1-based index (e.g. ``"Booklet 1"``, ``"Booklet 2"``).

    :param dest_file: Path to the destination FLAC file (must already exist).
    :param tags: The :class:`~music_annotator.models.TrackTags` instance to write.
    :param cover: Optional :class:`~music_annotator.models.CoverArt`; all available images are embedded when provided.
    :raises mutagen.MutagenError: If the file cannot be read or written.
    """
    audio = FLAC(str(dest_file))
    audio.clear()
    for key, value in tags.to_file_dict().items():
        audio[key.lower()] = value

    if cover and cover.available:
        _pic_groups: list[tuple[int, str, list[CoverImage]]] = [
            (3, "Cover", cover.front),
            (4, "Back", cover.back),
            (5, "Booklet", cover.booklet),
            (6, "Medium", cover.medium),
        ]
        for pic_type, label, images in _pic_groups:
            for idx, img in enumerate(images, start=1):
                pic = FLACPicture()  # type: ignore[no-untyped-call]
                pic.type = pic_type
                pic.mime = img.mime or "image/jpeg"
                pic.desc = f"{label} {idx}" if len(images) > 1 else label
                pic.width = pic.height = pic.depth = pic.colors = 0
                pic.data = img.data
                audio.add_picture(pic)  # type: ignore[no-untyped-call]

    audio.save()
    log.debug("tagged_flac", path=str(dest_file))


#: Standard ID3 text-frame keys written by :func:`apply_tags_mp3` (excluding ``TRACKNUMBER`` which uses special
#: ``N/total`` formatting handled separately).
_MP3_STD_KEYS: frozenset[str] = frozenset(
    {
        "TITLE",
        "ARTIST",
        "ALBUMARTIST",
        "ALBUM",
        "TRACKNUMBER",
        "DISCNUMBER",
        "DATE",
        "ORIGINALDATE",
        "COMPOSER",
        "CONDUCTOR",
        "ORGANIZATION",
    }
)

#: Mapping from uppercase tag-dict key to TXXX frame description string, used by both :func:`apply_tags_mp3` and
#: :func:`_read_tags_mp3` so that the same table drives both writing and read-back verification.
_MP3_TXXX_MAP: dict[str, str] = {
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


def apply_tags_mp3(dest_file: Path, tags: TrackTags, cover: CoverArt | None = None) -> None:
    """Write ID3v2.4 tags and all available cover art pictures to an MP3 file.

    Deletes any existing ID3 tags, writes standard text frames (``TIT2``, ``TPE1``, etc.) and ``TXXX`` frames for all
    non-internal non-empty fields, then embeds every image in ``cover`` as an ``APIC`` frame using the appropriate
    ID3 picture type for each CAA image category:

    - ``front`` images → APIC type 3 (``COVER_FRONT``)
    - ``back`` images → APIC type 4 (``COVER_BACK``)
    - ``booklet`` images → APIC type 5 (``LEAFLET_PAGE``)
    - ``medium`` images → APIC type 6 (``MEDIA``)

    All images within a category are embedded in listing order.  When multiple images share the same APIC type, each
    gets a unique ``desc`` suffixed with its 1-based index (e.g. ``"Booklet 1"``, ``"Booklet 2"``) so that ID3 frames,
    which are keyed by ``(type, desc)``, remain distinct.

    :param dest_file: Path to the destination MP3 file (must already exist).
    :param tags: The :class:`~music_annotator.models.TrackTags` instance to write.
    :param cover: Optional :class:`~music_annotator.models.CoverArt`; all available images are embedded when provided.
    :raises mutagen.MutagenError: If the file cannot be read or written.
    """
    try:
        audio = MP3(str(dest_file))
        if audio.tags:
            audio.tags.delete(str(dest_file))
    except Exception:  # noqa: BLE001
        pass

    id3_tags = ID3()  # type: ignore[no-untyped-call]
    file_dict = tags.to_file_dict()

    def txxx(desc: str, val: str) -> None:
        if val:
            id3_tags.add(TXXX(encoding=3, desc=desc, text=val))  # type: ignore[no-untyped-call]

    if file_dict.get("TITLE"):
        id3_tags.add(TIT2(encoding=3, text=file_dict["TITLE"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ARTIST"):
        id3_tags.add(TPE1(encoding=3, text=file_dict["ARTIST"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ALBUMARTIST"):
        id3_tags.add(TPE2(encoding=3, text=file_dict["ALBUMARTIST"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ALBUM"):
        id3_tags.add(TALB(encoding=3, text=file_dict["ALBUM"]))  # type: ignore[no-untyped-call]
    if file_dict.get("TRACKNUMBER"):
        total = file_dict.get("TOTALTRACKS", "")
        trck_text = f"{file_dict['TRACKNUMBER']}/{total}" if total else file_dict["TRACKNUMBER"]
        id3_tags.add(TRCK(encoding=3, text=trck_text))  # type: ignore[no-untyped-call]
    if file_dict.get("DISCNUMBER"):
        id3_tags.add(TPOS(encoding=3, text=file_dict["DISCNUMBER"]))  # type: ignore[no-untyped-call]
    if file_dict.get("DATE"):
        id3_tags.add(TDRC(encoding=3, text=file_dict["DATE"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ORIGINALDATE"):
        id3_tags.add(TDOR(encoding=3, text=file_dict["ORIGINALDATE"]))  # type: ignore[no-untyped-call]
    if file_dict.get("COMPOSER"):
        id3_tags.add(TCOM(encoding=3, text=file_dict["COMPOSER"]))  # type: ignore[no-untyped-call]
    if file_dict.get("CONDUCTOR"):
        id3_tags.add(TPE3(encoding=3, text=file_dict["CONDUCTOR"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ORGANIZATION"):
        id3_tags.add(TPUB(encoding=3, text=file_dict["ORGANIZATION"]))  # type: ignore[no-untyped-call]

    for meta_key, txxx_desc in _MP3_TXXX_MAP.items():
        txxx(txxx_desc, file_dict.get(meta_key, ""))

    if cover and cover.available:
        _apic_groups: list[tuple[int, str, list[CoverImage]]] = [
            (3, "Cover", cover.front),
            (4, "Back", cover.back),
            (5, "Booklet", cover.booklet),
            (6, "Medium", cover.medium),
        ]
        for apic_type, label, images in _apic_groups:
            for idx, img in enumerate(images, start=1):
                id3_tags.add(  # type: ignore[no-untyped-call]
                    APIC(  # type: ignore[no-untyped-call]
                        encoding=3,
                        mime=img.mime or "image/jpeg",
                        type=apic_type,
                        desc=f"{label} {idx}" if len(images) > 1 else label,
                        data=img.data,
                    )
                )

    id3_tags.save(str(dest_file), v2_version=4)
    log.debug("tagged_mp3", path=str(dest_file))


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def find_source_files(src_dir: Path) -> list[Path]:
    """Return a sorted list of audio files in ``src_dir``.

    Only the immediate children of ``src_dir`` are checked (not recursive).  Files are included when their lowercased
    suffix appears in :data:`AUDIO_EXTENSIONS`.

    :param src_dir: Directory to scan.
    :returns: A list of :class:`~pathlib.Path` objects for all matching files, sorted by filename.
    :raises OSError: If ``src_dir`` does not exist or is not readable.
    """
    return sorted(
        (p for p in src_dir.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda p: p.name,
    )


#: Filename of the JSON transaction journal written inside the destination root.
JOURNAL_FILENAME: str = "music_annotator_journal.json"


def write_transaction_log(journal_path: Path, new_entries: list[TransactionEntry]) -> None:
    """Append ``new_entries`` to the JSON transaction journal at ``journal_path``.

    If the journal file already exists and contains a valid JSON array of objects, the new entries are
    merged into the existing list before writing.  If the file is absent, corrupt, or empty it is
    (re-)created with only ``new_entries``.

    The journal is written atomically: entries are serialised to a temporary in-memory string first
    and the file is only opened for writing once the serialisation succeeds.

    :param journal_path: Absolute path of the journal file (typically
        ``<dest_root>/music_annotator_journal.json``).
    :param new_entries: The :class:`~music_annotator.models.TransactionEntry` objects to append.
    """
    existing: list[JSON] = []
    if journal_path.exists():
        try:
            raw = journal_path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                existing = parsed
        except (OSError, json.JSONDecodeError):
            log.warning("journal_corrupt_reset", path=str(journal_path))

    combined = TransactionLog(entries=[TransactionEntry.model_validate(e) for e in existing] + new_entries)
    journal_path.write_text(
        json.dumps([e.model_dump() for e in combined.entries], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("journal_written", path=str(journal_path), total=len(combined.entries))


def _check_collisions(dest_files: list[Path]) -> list[Path]:
    """Return the subset of ``dest_files`` that already exist on disk.

    Used by :func:`run` to identify which planned output files would be overwritten before copying
    begins, so the user can be warned and asked whether to proceed.

    :param dest_files: Ordered list of absolute destination paths to check.
    :returns: A (possibly empty) list of paths from ``dest_files`` that already exist.
    """
    return [p for p in dest_files if p.exists()]


def _sha256_file(path: Path) -> str:
    """Return the hex-encoded SHA-256 digest of the file at ``path``.

    Reads the file in 64 KiB chunks to avoid loading large audio files into memory at once.

    :param path: Path to the file to hash.
    :returns: A lowercase hexadecimal SHA-256 digest string.
    :raises OSError: If the file cannot be opened or read.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_tags_flac(path: Path) -> dict[str, str]:
    """Read Vorbis Comment tags from a FLAC file and return them in the same format as :meth:`~TrackTags.to_file_dict`.

    Only the first value of each multi-valued comment is returned.  Keys are uppercased to match :meth:`~TrackTags.to_file_dict`
    output.  The PICTURE block is excluded (cover art is verified separately in :func:`_verify_copy`).

    :param path: Path to the FLAC file to read.
    :returns: A ``{UPPERCASE_KEY: value}`` dict of non-empty tag values.
    :raises mutagen.MutagenError: If the file cannot be read.
    """
    audio = FLAC(str(path))
    return {k.upper(): v[0] for k, v in audio.items() if v and v[0]}


def _read_tags_mp3(path: Path) -> dict[str, str]:
    """Read ID3v2.4 tags from an MP3 file and return them in the same format as :meth:`~TrackTags.to_file_dict`.

    Reconstructs the standard text-frame fields (``TITLE``, ``ARTIST``, etc.) and all ``TXXX`` frames listed in
    :data:`_MP3_TXXX_MAP` from the inverse mapping.  The ``APIC`` cover-art frame is excluded (cover art is verified
    separately in :func:`_verify_copy`).  The ``TRACKNUMBER`` field is normalised to strip the ``/total`` suffix written
    by :func:`apply_tags_mp3` so that it can be compared directly to :meth:`~TrackTags.to_file_dict` output.

    :param path: Path to the MP3 file to read.
    :returns: A ``{UPPERCASE_KEY: value}`` dict of non-empty tag values.
    :raises mutagen.MutagenError: If the file cannot be read.
    """
    id3 = ID3(str(path))  # type: ignore[no-untyped-call]
    result: dict[str, str] = {}

    # Standard text frames
    _std_frames: dict[str, str] = {
        "TIT2": "TITLE",
        "TPE1": "ARTIST",
        "TPE2": "ALBUMARTIST",
        "TALB": "ALBUM",
        "TPOS": "DISCNUMBER",
        "TDRC": "DATE",
        "TDOR": "ORIGINALDATE",
        "TCOM": "COMPOSER",
        "TPE3": "CONDUCTOR",
        "TPUB": "ORGANIZATION",
    }
    for frame_id, tag_key in _std_frames.items():
        frame = id3.get(frame_id)  # type: ignore[no-untyped-call]
        if frame and str(frame):
            result[tag_key] = str(frame)

    # TRCK may be "N/total" — keep only the track number
    trck = id3.get("TRCK")  # type: ignore[no-untyped-call]
    if trck and str(trck):
        result["TRACKNUMBER"] = str(trck).split("/", maxsplit=1)[0]

    # TXXX frames — invert _MP3_TXXX_MAP (desc → tag_key)
    _txxx_inv = {desc: key for key, desc in _MP3_TXXX_MAP.items()}
    for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
        maybe_key: str | None = _txxx_inv.get(frame.desc)
        if maybe_key and frame.text and frame.text[0]:
            result[maybe_key] = frame.text[0]

    return {k: v for k, v in result.items() if v}


def _verify_copy(
    src_file: Path,
    dest_file: Path,
    tags: TrackTags,
    cover: CoverArt | None,
    src_mtime: float,
) -> None:
    """Verify that ``dest_file`` has been correctly tagged and has the expected modification time.

    Called by :func:`run` after :func:`apply_tags_flac` / :func:`apply_tags_mp3` and :func:`os.utime` have been applied.
    Raw copy integrity (SHA-256 equality before tagging) is verified inline in :func:`run` immediately after
    :func:`shutil.copy2`; this function handles the post-tagging checks:

    1. **Tag round-trip** — tags read back from ``dest_file`` match the expected :meth:`~TrackTags.to_file_dict` output.
    2. **Cover art** — if ``cover`` is provided and available, the embedded image bytes match ``cover.data``.
    3. **mtime** — ``dest_file`` modification time matches ``src_mtime`` (restored by :func:`os.utime`).

    :param src_file: Original source audio file (used for format detection and human-readable error messages).
    :param dest_file: Destination file to inspect.
    :param tags: The :class:`~music_annotator.models.TrackTags` instance that was written to ``dest_file``.
    :param cover: Optional :class:`~music_annotator.models.CoverArt`; cover bytes are verified when ``cover.available``.
    :param src_mtime: Expected ``st_mtime`` value as restored by :func:`os.utime`.
    :raises RuntimeError: If any verification check fails.
    """
    # 1. Tag round-trip
    ext = src_file.suffix.lower()
    match ext:
        case ".flac":
            actual_tags = _read_tags_flac(dest_file)
            expected_tags = tags.to_file_dict()
        case ".mp3":
            actual_tags = _read_tags_mp3(dest_file)
            # apply_tags_mp3 only writes standard frames + _MP3_TXXX_MAP keys; filter to that writable set.
            writable = _MP3_STD_KEYS | frozenset(_MP3_TXXX_MAP)
            expected_tags = {k: v for k, v in tags.to_file_dict().items() if k in writable}
        case _:  # pragma: no cover
            actual_tags = {}
            expected_tags = {}
    if actual_tags != expected_tags:
        missing = {k: expected_tags[k] for k in expected_tags if k not in actual_tags}
        extra = {k: actual_tags[k] for k in actual_tags if k not in expected_tags}
        wrong = {
            k: (expected_tags[k], actual_tags[k])
            for k in expected_tags
            if k in actual_tags and actual_tags[k] != expected_tags[k]
        }
        raise RuntimeError(
            f"tag verification failure for '{dest_file.name}': "
            f"missing={list(missing)}, extra={list(extra)}, wrong={list(wrong)}"
        )

    # 2. Cover art — build expected (pic_type, data) pairs from all CoverArt fields then compare to file.
    if cover and cover.available:
        expected_pics: list[tuple[int, bytes]] = []
        _cov_groups: list[tuple[int, list[CoverImage]]] = [
            (3, cover.front),
            (4, cover.back),
            (5, cover.booklet),
            (6, cover.medium),
        ]
        for pic_type, images in _cov_groups:
            for img in images:
                expected_pics.append((pic_type, img.data))

        match ext:
            case ".flac":
                actual_pics = [(p.type, p.data) for p in FLAC(str(dest_file)).pictures]
            case ".mp3":
                actual_pics = [
                    (f.type, f.data)
                    for f in ID3(str(dest_file)).getall("APIC")  # type: ignore[no-untyped-call]
                ]
            case _:  # pragma: no cover
                actual_pics = list(expected_pics)

        if actual_pics != expected_pics:
            raise RuntimeError(
                f"cover art verification failure for '{dest_file.name}': "
                f"expected {len(expected_pics)} picture(s), got {len(actual_pics)}"
            )

    # 3. mtime
    dest_mtime = dest_file.stat().st_mtime
    if dest_mtime != src_mtime:
        raise RuntimeError(f"mtime verification failure for '{dest_file.name}': expected {src_mtime}, got {dest_mtime}")


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
    5. Compute all destination paths and check for collisions with existing files.
       If any destination files already exist (and ``dry_run`` is ``False``), print a warning listing
       the conflicts and prompt the user:

       * ``o`` / ``overwrite`` — continue and overwrite all conflicting files.
       * ``s`` / ``skip`` — copy only the new files; leave existing files untouched.
       * ``a`` / ``abort`` — raise :exc:`SystemExit` without copying anything.

    6. Copy each source file to the destination tree, apply tags, and restore source timestamps.
    7. Append a :class:`~music_annotator.models.TransactionEntry` per file to
       ``<dest_root>/music_annotator_journal.json`` (created or updated atomically).

    :param release_id: The MusicBrainz release MBID.
    :param src_dir: Directory containing the source audio files.  Files are matched to release tracks by sorted filename
        order.  A count mismatch between source files and release tracks is logged as a warning but does not abort.
    :param dest_root: Root directory of the destination music library.
    :param user_agent: User-agent string passed to :func:`init_mb`.
    :param dry_run: When ``True``, log planned operations without copying or writing any files.  MB API calls for the
        release and recording relations still happen so the planned tag data is logged accurately.  The collision prompt
        and the journal write are also skipped.
    :param fetch_rels: When ``False``, skip per-recording lookups and produce minimal tags (faster but incomplete).
        Composer, conductor, work hierarchy, and Classical Extras tags will be absent.
    :raises mb.ResponseError: On a non-retryable MusicBrainz API error.
    :raises RuntimeError: If all retry attempts are exhausted for any API call, or if post-copy verification fails (copy
        integrity, tag round-trip, cover art, or mtime mismatch).
    :raises OSError: If source files cannot be read or destination files cannot be written.
    :raises SystemExit: With code 1 if the user chooses to abort when collisions are detected.
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

    # Fetch all cover art once for the whole release
    rg_id = release.release_group.id
    cover = CoverArt()
    if not dry_run:
        cover = fetch_cover_art(release_id, rg_id)
        if not cover.available:
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
                    if rel.work.id:
                        bottom_work = _get_bottom_work(rel.work)
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

    # Build the full (src_file, dest_file) plan before touching the filesystem.
    plan: list[tuple[int, Path, Path]] = []
    for idx, (src_file, (track, _medium_pos)) in enumerate(file_track_pairs):
        final_tags = tags_map[idx]
        dest_base = build_dest_path(dest_root, release, track, final_tags)
        dest_file = dest_base.with_suffix(src_file.suffix.lower())
        log.info("copy_track", src=src_file.name, dest=str(dest_file.relative_to(dest_root)))
        plan.append((idx, src_file, dest_file))

    # --- Collision detection and user prompt ---
    skip_dest: set[Path] = set()
    if not dry_run:
        collisions = _check_collisions([dest for _, _, dest in plan])
        if collisions:
            _console.print(f"\n[bold red]WARNING:[/] [red]{len(collisions)} destination file(s) already exist:[/]")
            for p in collisions:
                _console.print(f"  [red]{p}[/]")
            _console.print("\n[bold]Choose an action:[/]")
            _console.print("  [bold red]\\[a] abort[/]      — quit without copying anything")
            _console.print("  [bold yellow]\\[s] skip[/]       — copy only new files, leave existing untouched")
            _console.print("  [bold green]\\[o] overwrite[/]  — replace all existing files")
            while True:
                _console.print("[bold]Your choice \\[a/s/o]:[/] ", end="")
                choice = input("").strip().lower()
                match choice:
                    case "o" | "overwrite":
                        log.info("collision_choice_overwrite", count=len(collisions))
                        break
                    case "s" | "skip":
                        skip_dest = set(collisions)
                        log.info("collision_choice_skip", skipped=len(skip_dest))
                        break
                    case "a" | "abort":
                        log.warning("collision_choice_abort")
                        raise SystemExit(1)
                    case _:
                        _console.print("[yellow]Please enter 'a', 's', or 'o'.[/]")

    # --- Copy, tag, and journal ---
    journal_entries: list[TransactionEntry] = []
    now = datetime.datetime.now(datetime.UTC).isoformat()

    for idx, src_file, dest_file in plan:
        final_tags = tags_map[idx]

        if dry_run:
            log.info(
                "dry_run_track",
                composer=final_tags.composer,
                conductor=final_tags.conductor,
                work=final_tags.work,
                period=final_tags.period,
            )
            journal_entries.append(
                TransactionEntry(
                    timestamp=now,
                    release_id=release_id,
                    source=str(src_file),
                    destination=str(dest_file),
                    action="dry_run",
                )
            )
            continue

        if dest_file in skip_dest:
            log.info("skip_existing", dest=str(dest_file.relative_to(dest_root)))
            journal_entries.append(
                TransactionEntry(
                    timestamp=now,
                    release_id=release_id,
                    source=str(src_file),
                    destination=str(dest_file),
                    action="skipped",
                )
            )
            continue

        dest_file.parent.mkdir(parents=True, exist_ok=True)

        # Capture source timestamps and hash before copying; mutagen's .save() bumps mtime.
        # On Linux, ctime (inode-change time) cannot be set by userspace.
        src_stat = src_file.stat()
        src_times = (src_stat.st_atime, src_stat.st_mtime)
        src_hash = _sha256_file(src_file)

        shutil.copy2(src_file, dest_file)

        # Verify raw copy integrity before tagging mutates the destination.
        dest_copy_hash = _sha256_file(dest_file)
        if dest_copy_hash != src_hash:
            raise RuntimeError(
                f"copy integrity failure for '{dest_file.name}': "
                f"src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {dest_copy_hash[:12]}…"
            )

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

        _verify_copy(src_file, dest_file, final_tags, cover, src_stat.st_mtime)

        journal_entries.append(
            TransactionEntry(
                timestamp=now,
                release_id=release_id,
                source=str(src_file),
                destination=str(dest_file),
                action="copied",
            )
        )

    if not dry_run:
        write_transaction_log(dest_root / JOURNAL_FILENAME, journal_entries)

    log.info("run_complete", dest=str(dest_root))


# ---------------------------------------------------------------------------
# Discovery — identify MusicBrainz releases from a directory of tracks
# ---------------------------------------------------------------------------

#: Pattern matching a leading track-number prefix such as "01 - ", "02. ", "1 - ".
_TRACK_PREFIX_RE: re.Pattern[str] = re.compile(r"^\d{1,3}[\s.\-–]+")

#: FreeDB hex CRC suffix appended to directory names, e.g. ".0xe212b212".
_FREEDB_HEX_SUFFIX_RE: re.Pattern[str] = re.compile(r"\.\s*0x[0-9a-fA-F]+$")

#: Disc-number suffix such as "(Disc 1)", "(disc 2)", or "Disc 1" at the end of a dir name.
_DISC_SUFFIX_RE: re.Pattern[str] = re.compile(r"\s*[\(\[]?[Dd]isc\s*\d+[\)\]]?\s*$")

#: Bracketed annotation such as "[1980s]" or "[Marriner]".
_BRACKET_RE: re.Pattern[str] = re.compile(r"\s*\[[^\]]*\]")

#: MB base URL for release pages.
_MB_RELEASE_URL = "https://musicbrainz.org/release/"

#: Filename of the FreeDB disc-info YAML file written alongside ripped tracks.
_DISC_INFO_FILENAME = "00 - disc info.yaml"


def parse_disc_info_yaml(src_dir: Path) -> tuple[str, str] | None:  # pylint: disable=too-many-return-statements
    """Extract a ``(query, artist)`` pair from a FreeDB ``00 - disc info.yaml`` file.

    The file contains a ``record`` list of FreeDB entries for the disc.  Each entry has a ``track_info`` dict with a ``DTITLE``
    key whose value is ``"artist / title"`` — the `` / `` separator is the FreeDB standard.  When multiple records are present
    the one marked ``preferred: true`` is used; if none is marked preferred the first record is used.

    :param src_dir: Directory that may contain a ``00 - disc info.yaml`` file.
    :returns: A ``(query, artist)`` tuple if a usable ``DTITLE`` is found, or ``None`` if the file is absent, the record list is
        empty, or ``DTITLE`` is missing / blank.
    :raises yaml.YAMLError: Propagated if the file exists but cannot be parsed.
    """
    yaml_path = src_dir / _DISC_INFO_FILENAME
    if not yaml_path.is_file():
        return None

    with yaml_path.open(encoding="utf-8") as fh:
        data: object = yaml.full_load(fh)

    if not isinstance(data, dict):
        return None

    records: object = data.get("record")
    if not isinstance(records, list) or not records:
        return None

    # Prefer the record explicitly marked preferred; fall back to the first.
    preferred: dict[str, object] | None = None
    for rec in records:
        if isinstance(rec, dict) and rec.get("preferred"):
            preferred = rec
            break
    if preferred is None:
        preferred = records[0] if isinstance(records[0], dict) else None
    if preferred is None:
        return None

    track_info: object = preferred.get("track_info")
    if not isinstance(track_info, dict):
        return None

    dtitle = str(track_info.get("DTITLE", "")).strip()
    if not dtitle:
        return None

    # FreeDB DTITLE format is "artist / title"; fall back to the whole string as the query.
    if " / " in dtitle:
        artist, title = dtitle.split(" / ", 1)
        return title.strip(), artist.strip()
    return dtitle, ""


def _parse_disc_id_list(disc_id: list[object]) -> tuple[int, int, list[int]] | None:
    """Validate and decode a FreeDB ``disc_id`` list into ``(num_tracks, leadout_frame, track_frames)``.

    The expected structure is ``[freedb_crc, num_tracks, offset_1, …, offset_N, total_seconds]`` where ``total_seconds * 75``
    gives the MusicBrainz lead-out frame address.

    :param disc_id: The raw ``disc_id`` list from the YAML document.
    :returns: A ``(num_tracks, leadout_frame, track_frames)`` triple, or ``None`` when the list is malformed (too short, wrong
        types, or mismatched offset count).
    """
    # Minimum viable list: [crc, num_tracks, offset_1, total_seconds] → length 4
    if len(disc_id) < 4:  # noqa: PLR2004
        return None
    num_tracks_raw = disc_id[1]
    total_seconds_raw = disc_id[-1]
    if not isinstance(num_tracks_raw, int) or num_tracks_raw < 1:
        return None
    if not isinstance(total_seconds_raw, int) or total_seconds_raw < 1:
        return None
    offsets_raw: list[object] = disc_id[2:-1]
    if len(offsets_raw) != num_tracks_raw:
        return None
    track_frames: list[int] = []
    for offset in offsets_raw:
        if not isinstance(offset, int):
            return None
        track_frames.append(offset)
    return num_tracks_raw, total_seconds_raw * 75, track_frames


def parse_disc_toc(src_dir: Path) -> tuple[int, int, list[int]] | None:
    """Extract the CD table-of-contents from a FreeDB ``00 - disc info.yaml`` file.

    The ``disc_id`` field in the YAML is a list with the structure::

        [freedb_crc, num_tracks, offset_1, offset_2, …, offset_N, total_seconds]

    where:

    * ``freedb_crc`` — the FreeDB CRC checksum (element 0, ignored here).
    * ``num_tracks`` — number of audio tracks (element 1).
    * ``offset_1 … offset_N`` — per-track frame offsets in CD frames (elements 2 through N+1).
    * ``total_seconds`` — the disc's total playing time in seconds; multiplying by 75 gives the lead-out frame address as
      expected by the MusicBrainz disc-ID and TOC lookup APIs.

    :param src_dir: Directory that may contain a ``00 - disc info.yaml`` file.
    :returns: A ``(num_tracks, leadout_frame, track_frames)`` triple when a valid TOC is found, or ``None`` if the file is
        absent, the ``disc_id`` key is missing, or the list is too short to contain at least one track offset.
    :raises yaml.YAMLError: Propagated if the file exists but cannot be parsed.
    """
    yaml_path = src_dir / _DISC_INFO_FILENAME
    if not yaml_path.is_file():
        return None

    with yaml_path.open(encoding="utf-8") as fh:
        data: object = yaml.full_load(fh)

    if not isinstance(data, dict):
        return None

    disc_id: object = data.get("disc_id")
    if not isinstance(disc_id, list):
        return None

    return _parse_disc_id_list(disc_id)


def _toc_lookup_mb_releases(toc_string: str, limit: int) -> list[dict[str, object]]:
    """Query MusicBrainz for releases matching a CD TOC string.

    Calls ``mb.get_releases_by_discid`` with ``toc=toc_string`` so that the MB server performs a fuzzy TOC match even when the
    exact disc ID is not in the database.  The call uses a sentinel disc ID (``"intentionally-invalid-id"``) that will never
    match, ensuring the server always falls through to the fuzzy TOC path and returns a ``"release-list"`` dict.

    Response shapes handled:

    * ``{"disc": {"release-list": [...]}}`` — exact disc-ID match (rare).
    * ``{"release-list": [...], "release-count": N}`` — fuzzy TOC match (typical).
    * :class:`~musicbrainzngs.ResponseError` with a ``"404"`` status — no matches; return ``[]``.

    :param toc_string: A TOC string in the form ``"1 <num_tracks> <leadout_frame> <offset_1> … <offset_N>"``.
    :param limit: Maximum number of results to slice from the response list.
    :returns: A list of raw release dicts (possibly empty).
    """

    @_mb_retry
    def _call() -> dict[str, object]:
        return mb.get_releases_by_discid(  # type: ignore[no-any-return]
            "intentionally-invalid-id",
            toc=toc_string,
            includes=["artist-credits", "labels"],
        )

    try:
        response: dict[str, object] = _call()
    except mb.ResponseError as exc:
        if "404" in str(exc):
            return []
        raise

    # Exact match path: {"disc": {"release-list": [...]}}
    disc: object = response.get("disc")
    if isinstance(disc, dict):
        release_list: object = disc.get("release-list", [])
        if isinstance(release_list, list):
            return [r for r in release_list if isinstance(r, dict)][:limit]

    # Fuzzy / TOC path: {"release-list": [...]}
    fuzzy_list: object = response.get("release-list", [])
    if isinstance(fuzzy_list, list):
        return [r for r in fuzzy_list if isinstance(r, dict)][:limit]

    return []


def _score_toc_release(item: Mapping[str, object], expected_tracks: int) -> int:
    """Synthesise a relevance score (0–100) for a TOC lookup result.

    TOC results carry no ``ext:score`` field; this function approximates quality by measuring how well the disc that triggered
    the match fits the expected track count.

    Scoring logic:

    * If exactly one medium in the release has ``track-count == expected_tracks``, score = 100.
    * If multiple media match, score is reduced proportionally by the total number of media.
    * If no medium matches the expected count, the release scores 0.

    :param item: Raw release dict from a TOC/disc-ID MB response.
    :param expected_tracks: Number of audio tracks on the local disc.
    :returns: An integer score in the range 0–100.
    """
    medium_list: object = item.get("medium-list", [])
    if not isinstance(medium_list, list):
        return 0

    total_media = len(medium_list)
    if total_media == 0:
        return 0

    matching_media = 0
    for medium in medium_list:
        if not isinstance(medium, dict):
            continue
        # TOC responses carry 'track-count' as an int on each medium.
        tc_raw: object = medium.get("track-count")
        if isinstance(tc_raw, int) and tc_raw == expected_tracks:
            matching_media += 1
        elif not isinstance(tc_raw, int):
            # Fall back to counting track-list entries if track-count absent.
            tl: object = medium.get("track-list", [])
            if isinstance(tl, list) and len(tl) == expected_tracks:
                matching_media += 1

    if matching_media == 0:
        return 0

    # Perfect single-disc match → 100; penalise box sets by media count.
    raw_score = 100 * matching_media // total_media
    return min(raw_score, 100)


def parse_dir_hint(src_dir: Path) -> tuple[str, str]:
    """Extract a ``(query, "")`` pair from a source directory name and its track filenames.

    FreeDB directory names follow no consistent ``"artist - album"`` ordering — the same library may have ``"Beethoven
    Symphonies - Karajan"`` next to ``"Karajan - Beethoven Symphonies"``.  Attempting to split on `` - `` produces unreliable
    results, so the entire cleaned directory name is returned as a single query string with no separate artist hint.

    Cleaning steps applied to the directory name:

    * Strip the FreeDB hex CRC suffix (e.g. ``.0xe212b212``).
    * Strip disc-number suffixes (e.g. ``(Disc 1)``).
    * Replace ``::`` (used in this library as a path-safe substitute for ``/``) with a space.
    * Strip ``[bracketed]`` annotations (e.g. ``[1980s]``, ``[Marriner]``).

    When the cleaned result is very short (fewer than 4 characters), the audio file stems in the directory are examined:
    track-number prefixes are stripped and the longest remaining stem is used instead.

    :param src_dir: Directory containing the source audio files.
    :returns: A ``(query, artist_hint)`` tuple; ``artist_hint`` is always ``""`` because the naming convention does not reliably
        distinguish artist from title.
    """
    raw = src_dir.name
    query = _FREEDB_HEX_SUFFIX_RE.sub("", raw)
    query = _DISC_SUFFIX_RE.sub("", query)
    query = query.replace("::", " ")
    query = _BRACKET_RE.sub("", query)
    query = query.strip().strip("-").strip()

    if len(query) < 4:  # noqa: PLR2004
        stems = [_TRACK_PREFIX_RE.sub("", f.stem) for f in find_source_files(src_dir)]
        if stems:
            query = max(stems, key=len)

    return query, ""


def _search_mb_releases(query: str, tracks: int, limit: int) -> dict[str, JSON]:
    """Call ``mb.search_releases`` and return the raw response dict.

    Wraps the call with the ``_mb_retry`` decorator indirectly by delegating to a decorated inner function, so transient 503/429
    errors are automatically retried.

    :param query: Lucene query string for the ``release`` field.
    :param tracks: Expected total track count; added as a ``tracks`` field constraint when non-zero.
    :param limit: Maximum number of results to return.
    :returns: Raw ``musicbrainzngs`` response dict containing a ``"release-list"`` key.
    """

    @_mb_retry
    def _call() -> dict[str, JSON]:
        if tracks:
            return mb.search_releases(query, limit=limit, tracks=tracks)  # type: ignore[no-any-return]
        return mb.search_releases(query, limit=limit)  # type: ignore[no-any-return]

    return _call()


def _parse_release_item(item: Mapping[str, object], score: int) -> MBReleaseCandidate:
    """Convert a raw MB release dict into a :class:`~music_annotator.models.MBReleaseCandidate`.

    Derives the total track count by summing ``track-list`` lengths across all media (with ``track-count`` used when
    ``track-list`` is absent, as in TOC responses).  The format string is taken from the first medium that declares one.

    :param item: A raw release dict from a ``musicbrainzngs`` response.
    :param score: Relevance score (0–100) to assign to the candidate; callers supply this because text-search results carry
        ``ext:score`` while TOC results do not.
    :returns: A populated :class:`~music_annotator.models.MBReleaseCandidate`.
    """
    medium_list: object = item.get("medium-list", [])
    total_tracks = 0
    fmt = ""
    if isinstance(medium_list, list):
        for medium in medium_list:
            if not isinstance(medium, dict):
                continue
            tl: object = medium.get("track-list")
            if isinstance(tl, list):
                total_tracks += len(tl)
            else:
                # TOC responses carry track-count as an int instead of track-list.
                tc_raw: object = medium.get("track-count")
                if isinstance(tc_raw, int):
                    total_tracks += tc_raw
            if not fmt:
                fmt = str(medium.get("format", ""))

    label_info_list: object = item.get("label-info-list", [])
    label_name = ""
    cat_num = ""
    if isinstance(label_info_list, list) and label_info_list and isinstance(label_info_list[0], dict):
        first_label_info: dict[str, object] = label_info_list[0]
        label_dict = first_label_info.get("label", {})
        label_name = str(label_dict.get("name", "")) if isinstance(label_dict, dict) else ""
        cat_num = str(first_label_info.get("catalog-number", ""))

    release_id = str(item.get("id", ""))
    return MBReleaseCandidate(
        release_id=release_id,
        score=score,
        title=str(item.get("title", "")),
        artist=str(item.get("artist-credit-phrase", "")),
        date=str(item.get("date", "")),
        format=fmt,
        tracks=total_tracks,
        label=label_name,
        catalog_number=cat_num,
        country=str(item.get("country", "")),
        status=str(item.get("status", "")),
        mb_url=f"{_MB_RELEASE_URL}{release_id}" if release_id else "",
    )


def search_releases_by_dir(src_dir: Path, limit: int = 10) -> list[MBReleaseCandidate]:
    """Search MusicBrainz for releases matching a source directory of audio tracks.

    Query derivation strategy (in priority order):

    1. **TOC lookup**: if ``00 - disc info.yaml`` contains a ``disc_id`` list with valid track offsets, build a MusicBrainz TOC
       string and call :func:`_toc_lookup_mb_releases`.  Scores are synthesised by :func:`_score_toc_release` based on
       track-count match quality.
    2. **DTITLE text search**: if the YAML exists but has no valid TOC, use the ``DTITLE`` from the preferred FreeDB record via
       :func:`parse_disc_info_yaml`.
    3. **Directory-name text search**: fall back to :func:`parse_dir_hint`, which cleans the directory name and returns it as a
       free-text query.

    :param src_dir: Directory containing the source audio files.
    :param limit: Maximum number of candidates to return (passed to the MB search API).
    :returns: List of :class:`~music_annotator.models.MBReleaseCandidate` sorted by score descending.
    :raises ValueError: If ``src_dir`` contains no recognised audio files.
    """
    source_files = find_source_files(src_dir)
    if not source_files:
        raise ValueError(f"no audio files found in {src_dir}")

    track_count = len(source_files)

    # --- Priority 1: TOC lookup ---
    toc = parse_disc_toc(src_dir)
    if toc is not None:
        num_tracks, leadout_frame, track_frames = toc
        toc_string = f"1 {num_tracks} {leadout_frame} " + " ".join(str(o) for o in track_frames)
        log.debug("toc_lookup", toc_string=toc_string, src_dir=str(src_dir))
        raw_items = _toc_lookup_mb_releases(toc_string, limit)
        if raw_items:
            candidates: list[MBReleaseCandidate] = []
            for item in raw_items:
                score = _score_toc_release(item, num_tracks)
                candidates.append(_parse_release_item(item, score))
            candidates.sort(key=lambda c: c.score, reverse=True)
            return candidates

    # --- Priority 2: DTITLE text search ---
    hint = parse_disc_info_yaml(src_dir)
    if hint is not None:
        query, _ = hint
        log.debug("disc_info_yaml_hint", query=query, src_dir=str(src_dir))
    else:
        # --- Priority 3: directory name text search ---
        query, _ = parse_dir_hint(src_dir)
        if not query:  # pragma: no cover
            query = src_dir.name

    log.debug("mb_search_releases", query=query, tracks=track_count, limit=limit)
    raw = _search_mb_releases(query, track_count, limit)

    release_list: object = raw.get("release-list", [])
    candidates = []
    if isinstance(release_list, list):
        for item in release_list:
            if not isinstance(item, dict):
                continue
            raw_score = item.get("ext:score", 0)
            score = int(raw_score) if isinstance(raw_score, (int, float, str)) else 0
            candidates.append(_parse_release_item(item, score))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def _format_candidate(index: int, candidate: MBReleaseCandidate) -> str:
    """Format a single :class:`~music_annotator.models.MBReleaseCandidate` as a human-readable numbered entry.

    :param index: 1-based display index.
    :param candidate: The candidate to format.
    :returns: A multi-line string ready to print to stdout.
    """
    lines = [
        f"  [bold yellow]\\[{index}][/] [dim]score={candidate.score}[/]  [bold white]{candidate.title}[/]",
        f"  [dim]     artist :[/] {candidate.artist or '(unknown)'}",
        f"  [dim]     date   :[/] {candidate.date or '(unknown)'}",
        f"  [dim]     format :[/] {candidate.format or '(unknown)'}  tracks={candidate.tracks}",
        f"  [dim]     status :[/] {candidate.status or '(unknown)'}  country={candidate.country or '?'}",
        f"  [dim]     label  :[/] {candidate.label or '(none)'}  catno={candidate.catalog_number or '(none)'}",
        f"  [dim]     url    :[/] [dim cyan]{candidate.mb_url}[/]",
    ]
    return "\n".join(lines)


def discover(
    src_dirs: list[Path],
    dest_root: Path,
    user_agent: str,
    dry_run: bool = False,
    fetch_rels: bool = True,
    limit: int = 10,
) -> None:
    """Search MusicBrainz for releases matching each source directory, prompt for confirmation, then apply tags.

    For each directory in ``src_dirs`` the function:

    1. Searches MB using :func:`search_releases_by_dir` and prints a numbered candidate list.
    2. Prompts the user to enter a candidate number, a raw MBID, or ``s`` to skip.
    3. If a valid selection is made, calls :func:`run` to copy and tag that directory.
    4. After a successful copy (unless ``dry_run`` is set), prompts the user to delete the original source directory.

    :param src_dirs: List of source directories to process in order.
    :param dest_root: Root destination directory for the annotated music library.
    :param user_agent: MusicBrainz user-agent string.
    :param dry_run: When ``True``, pass through to :func:`run` without writing files; the delete prompt is suppressed.
    :param fetch_rels: When ``False``, skip per-recording relation lookups in :func:`run`.
    :param limit: Maximum number of search candidates to display per directory.
    """
    init_mb(user_agent)
    for src_dir in src_dirs:
        log.info("discover_dir", path=str(src_dir))
        rule = f"[bold cyan]{'=' * 72}[/]"
        _console.print(f"\n{rule}")
        _console.print(f"[bold cyan]Directory:[/] [bold]{src_dir}[/]")
        _console.print(rule)

        try:
            candidates = search_releases_by_dir(src_dir, limit=limit)
        except ValueError as exc:
            log.warning("discover_skip", reason=str(exc))
            _console.print(f"  [yellow]Skipped:[/] {exc}")
            continue

        if not candidates:
            _console.print("  [yellow]No candidates found.[/]")
            continue

        for i, candidate in enumerate(candidates, 1):
            _console.print(_format_candidate(i, candidate))
            _console.print()

        _console.print(f"  [bold]Enter a number (1–{len(candidates)}), a raw MBID, or 's' to skip:[/]")
        _console.print("  [bold]>[/] ", end="")
        choice = input("").strip()

        if choice.lower() in {"s", "skip", ""}:
            log.info("discover_skipped", path=str(src_dir))
            continue

        release_id: str
        # Check if numeric choice
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(candidates):
                release_id = candidates[idx].release_id
            else:
                _console.print(f"  [yellow]Invalid selection '{choice}', skipping.[/]")
                continue
        else:
            # Treat as raw MBID
            release_id = choice

        log.info("discover_selected", release_id=release_id, src_dir=str(src_dir))
        try:
            run(
                release_id=release_id,
                src_dir=src_dir,
                dest_root=dest_root,
                user_agent=user_agent,
                dry_run=dry_run,
                fetch_rels=fetch_rels,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("discover_run_error", release_id=release_id, error=str(exc), exc_info=True)
            continue

        if not dry_run:
            _console.print(f"\n  [bold]Delete original directory[/] [bold red]{src_dir}[/][bold]?[/] [dim](y/n)[/] ", end="")
            if input("").strip().lower() in {"y", "yes"}:
                shutil.rmtree(src_dir)
                log.info("discover_deleted_src", path=str(src_dir))
            else:
                log.info("discover_kept_src", path=str(src_dir))
