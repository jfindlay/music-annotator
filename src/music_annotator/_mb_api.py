"""MusicBrainz API helpers for music-annotator.

Provides retry-decorated wrappers around ``musicbrainzngs`` functions plus the AcoustID lookup.
The module-level :data:`_WORK_CACHE` avoids redundant round-trips for shared parent works.
"""

from __future__ import annotations

import functools
import json
import time
import urllib.request
import xml.etree.ElementTree as _ET
from collections.abc import Callable
from typing import ParamSpec, TypeVar

import musicbrainzngs as mb
import musicbrainzngs.mbxml as _mbxml
import structlog

from music_annotator.models import JSON, CoverArt, CoverImage, MBRecording, MBRelease, MBWork

# ---------------------------------------------------------------------------
# Workaround for a musicbrainzngs bug: parse_recording omits "first-release-date"
# from its elements list, so the field is present in the MB XML response but
# silently discarded.  The patch below re-reads it directly from the XML element
# after the original parser runs.
#
# Upstream fix: add "first-release-date" to the elements list in mbxml.parse_recording.
# Remove this patch once musicbrainzngs (or its successor musicbrainzngs2) ships the fix.
# ---------------------------------------------------------------------------


_MBXML_NS = "http://musicbrainz.org/ns/mmd-2.0#"

# Capture the original unpatched function before we replace it below.
_mbxml_original_parse_recording = _mbxml.parse_recording  # noqa: N816


def _patched_parse_recording(recording: _ET.Element) -> dict[str, JSON]:
    """Replacement for ``musicbrainzngs.mbxml.parse_recording`` that recovers ``first-release-date``.

    musicbrainzngs omits ``"first-release-date"`` from its ``parse_recording`` elements list,
    so the field is present in the MB XML response but silently discarded.  This wrapper calls
    the original parser and then reads the field directly from the XML element, adding it to
    the result dict when present and non-empty.

    Remove this function once musicbrainzngs (or its successor musicbrainzngs2) ships the fix
    (add ``"first-release-date"`` to the elements list in ``mbxml.parse_recording``).

    :param recording: The ``<recording>`` XML element from the MB API response.
    :returns: Parsed recording dict, augmented with ``first-release-date`` when present.
    """
    result: dict[str, JSON] = _mbxml_original_parse_recording(recording)
    frd = recording.find(f"{{{_MBXML_NS}}}first-release-date")
    if frd is not None and frd.text:
        result["first-release-date"] = frd.text
    return result


_mbxml.parse_recording = _patched_parse_recording

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: In-process cache: work_id → MBWork, avoids redundant API calls for shared parents.
_WORK_CACHE: dict[str, MBWork] = {}

_P = ParamSpec("_P")
_T = TypeVar("_T")


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


def _mb_call(fn: Callable[[], _T]) -> _T:
    """Call ``fn()`` and sleep 1 second to respect the MB / CAA 1 req/s rate limit.

    Consolidates the repeated ``result = api_call(); time.sleep(1)`` pattern that appears at every
    non-retry MB or Cover Art Archive call site.  The backoff sleep inside :func:`_mb_retry` is
    intentionally separate and not affected by this helper.

    :param fn: A zero-argument callable that performs exactly one MB or CAA network request.
    :returns: The return value of ``fn()``.
    """
    result = fn()
    time.sleep(1)
    return result


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
    result = _mb_call(lambda: _get_release_by_id(release_id))
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
    result = _mb_call(lambda: _get_recording_by_id(recording_id))
    return MBRecording.model_validate(result.get("recording", {}))


def fetch_cover_art(release_id: str, release_group_id: str = "") -> CoverArt:
    """Download all available cover art for a release from the Cover Art Archive.

    Strategy:

    1. Call ``mb.get_image_list(release_id)`` to obtain the full CAA image listing for the release.
    2. For each image entry in the listing, classify it by its ``types`` list into one of: ``front``
       (``"Front"``), ``back`` (``"Back"``), ``booklet`` (``"Booklet"``), or ``medium`` (``"Medium"``).
       Images whose types do not include any of these four strings are skipped.
    3. Fetch the binary data for each classified image via ``mb.get_image(release_id, coverid)`` at
       original resolution (no ``size`` argument), sleeping 1 second after each network call to respect
       the 1 req/s rate limit.
    4. If the release has no CAA listing (HTTP 404) and ``release_group_id`` is provided, fall back to
       fetching the release-group front image via ``mb.get_release_group_image_front()`` at original
       resolution and place it in ``front`` only.

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
            raw = _mb_call(lambda: mb.get_image(rel_id, coverid))
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
                    log.warning("cover_art_skipped_type", types=types, coverid=str(coverid))

    # If no release listing was available, fall back to the release-group front image.
    if not has_release_listing and release_group_id:
        try:
            raw = _mb_call(lambda: mb.get_release_group_image_front(release_group_id))
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
    result = _mb_call(lambda: _get_work_by_id(work_id))
    work = MBWork.model_validate(result.get("work", {}))
    _WORK_CACHE[work_id] = work
    return work


def fetch_acoustid_id(recording_mbid: str) -> str:
    """Look up the AcoustID track ID (UUID) for a MusicBrainz recording MBID.

    Calls ``https://api.acoustid.org/v2/track/list_by_mbid`` with the recording MBID and returns the first AcoustID track ID
    UUID from the response.  The AcoustID track ID is a cluster identifier that groups all crowd-sourced Chromaprint fingerprint
    submissions for the same track.  It is stored in the ``ACOUSTID_ID`` tag (Vorbis Comment) / TXXX ``"Acoustid Id"`` frame
    (ID3), matching the convention used by MusicBrainz Picard.

    No API key is required for this endpoint.  The call uses a 10-second socket timeout.  Up to three attempts are made on
    transient network errors (``OSError``), sleeping ``2 ** attempt`` seconds between retries.  A ``JSONDecodeError`` (malformed
    response) is not retried because the response content is unlikely to change.  The function always returns ``""`` on failure
    so that the rest of the annotation pipeline is never blocked by AcoustID being unavailable.  On success a 1-second polite
    delay is observed before returning.

    :param recording_mbid: The MusicBrainz recording MBID (UUID string).
    :returns: The first AcoustID track ID UUID string, or ``""`` when none is found or the request fails.
    """
    log.debug("fetch_acoustid_id", recording_mbid=recording_mbid)
    url = f"https://api.acoustid.org/v2/track/list_by_mbid?mbid={recording_mbid}&format=json"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                raw = resp.read()
            time.sleep(1)
            data: JSON = json.loads(raw)
            if not isinstance(data, dict):
                return ""
            tracks = data.get("tracks")
            if not isinstance(tracks, list) or not tracks:
                return ""
            first = tracks[0]
            if not isinstance(first, dict):
                return ""
            track_id = first.get("id", "")
            return str(track_id) if track_id else ""
        except json.JSONDecodeError:
            log.warning("acoustid_parse_failed", recording_mbid=recording_mbid)
            return ""
        except OSError as exc:
            wait = 2**attempt
            log.warning("acoustid_lookup_failed", recording_mbid=recording_mbid, attempt=attempt, wait_s=wait, error=str(exc))
            time.sleep(wait)
    return ""


def _get_bottom_work(embedded: MBWork) -> MBWork:
    """Return the bottom work for a performance relation, using inlined data when available.

    When ``musicbrainzngs`` is called with the ``work-level-rels`` include, the MB API inlines the full work detail (including
    its own ``artist-relation-list`` and ``work-relation-list``) directly inside the recording response.  In that case
    ``embedded`` already carries all the data needed and no extra network round-trip is required.

    If ``embedded`` has empty relation lists (stub shape — ``work-level-rels`` was absent or the library did not parse the
    inlined data), fall back to :func:`fetch_work_detail`.

    :param embedded: The :class:`~music_annotator.models.MBWork` extracted from the recording's
        performance ``work-relation-list`` entry.
    :returns: A fully populated :class:`~music_annotator.models.MBWork`.
    """
    if embedded.artist_relation_list or embedded.work_relation_list:
        log.debug("bottom_work_inlined", work_id=embedded.id)
        return embedded
    log.debug("fetch_bottom_work", work_id=embedded.id)
    return fetch_work_detail(embedded.id)
