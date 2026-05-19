"""MusicBrainz API helpers for music-annotator.

Provides retry-decorated wrappers around ``musicbrainzngs`` functions plus the AcoustID lookup.
The module-level :data:`_WORK_CACHE` avoids redundant round-trips for shared parent works.
"""

from __future__ import annotations

import functools
import json
import os
import socket
import time
import urllib.request
import xml.etree.ElementTree as _ET
from collections.abc import Callable
from pathlib import Path
from typing import ParamSpec, Protocol, TypeVar

import musicbrainzngs as mb
import musicbrainzngs.compat as _mbcompat
import musicbrainzngs.mbxml as _mbxml
import musicbrainzngs.musicbrainz as _mbmz
import structlog

from music_annotator.models import JSON, CoverArt, CoverImage, MBArtistRelation, MBRecording, MBRelease, MBWork

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

# ---------------------------------------------------------------------------
# Workaround for a musicbrainzngs bug: _safe_read retries any "unknown" HTTP
# status code (including 307 redirect loops detected by Python's redirect
# handler) up to 8 times with growing delays (~60 s total) before raising
# NetworkError.  The correct behaviour for any non-retryable status is to fail
# immediately with ResponseError.
#
# The patch below replaces the ``else: # retrying for now`` branch in
# _safe_read with ``raise ResponseError(cause=exc)``.  Retryable codes (503,
# 502, 500) and auth failures (401) are left unchanged.
#
# Upstream fix: mbngs2-1 — replace the else branch in _safe_read.
# Remove this patch once musicbrainzngs2 ships the fix.
# ---------------------------------------------------------------------------

# Aliases for private musicbrainzngs.musicbrainz members used by the patch.  The # noqa
# suppresses ruff SLF001; the pylint disable suppresses the equivalent W0212 warning.
# Both are intentional: the monkey-patch must mirror the library's own internal calls.
_mbmz_original_safe_read = _mbmz._safe_read  # noqa: SLF001  # pylint: disable=protected-access
_mbmz_log = _mbmz._log  # noqa: SLF001  # pylint: disable=protected-access


class _HttpResponse(Protocol):
    """Structural interface for the file-like object returned by an opener's ``open()`` call.

    Only the ``read()`` method is used by ``_patched_safe_read``; the rest of the HTTP response
    interface is irrelevant to the patch.
    """

    def read(self) -> bytes:
        """Read and return the response body as bytes.

        :returns: The raw response bytes.
        """


class _HttpOpener(Protocol):
    """Structural interface for the urllib opener used by ``_patched_safe_read``.

    musicbrainzngs passes an ``OpenerDirector`` instance here; the only method exercised
    by ``_safe_read`` is ``open``.  The Protocol avoids ``object`` in the signature while
    keeping the patch free of ``Any``.
    """

    def open(self, req: object, body: object = None) -> _HttpResponse:
        """Open an HTTP request, optionally with a request body.

        :param req: The request object.
        :param body: Optional POST body.
        :returns: An HTTP response object whose ``read()`` yields the body bytes.
        """


def _patched_safe_read(
    opener: _HttpOpener,
    req: object,
    body: object = None,
    max_retries: int = 8,
    retry_delay_delta: float = 2.0,
) -> bytes:
    """Replacement for ``musicbrainzngs.musicbrainz._safe_read`` that fails fast on non-retryable HTTP errors.

    The original ``_safe_read`` retries any HTTP status code not explicitly handled (including 307
    redirect loops) up to ``max_retries`` times with growing delays, producing a hang of up to
    ~60 seconds before raising ``NetworkError``.  This patch raises ``ResponseError`` immediately
    for such codes instead, matching the user's preference that data fetch errors are fatal and fast.

    Retryable codes (503, 502, 500) and auth errors (401) are handled identically to the original.

    Remove this function once musicbrainzngs2 ships the upstream fix (mbngs2-1: replace the
    ``else: retrying for now`` branch with ``raise ResponseError(cause=exc)``).

    :param opener: The ``urllib`` opener used to perform the HTTP request.
    :param req: The ``urllib.request.Request`` object describing the request.
    :param body: Optional request body bytes (for POST requests).
    :param max_retries: Maximum number of attempts before raising ``NetworkError``.
    :param retry_delay_delta: Seconds added per retry for the increasing delay.
    :returns: The raw response bytes on success.
    :raises mb.ResponseError: On any permanent HTTP error (400, 404, 411, or any unknown code).
    :raises mb.AuthenticationError: On HTTP 401.
    :raises mb.NetworkError: After all retries are exhausted or on a network-level failure.
    """
    last_exc: Exception | None = None
    for retry_num in range(max_retries):
        if retry_num:
            _mbmz_log.info(f"retrying after delay (#{retry_num})")
            time.sleep(retry_num * retry_delay_delta)
        try:
            f = opener.open(req, body) if body else opener.open(req)
            return f.read()
        except _mbcompat.HTTPError as exc:
            if exc.code in (400, 404, 411):
                raise _mbmz.ResponseError(cause=exc)
            if exc.code in (503, 502, 500):
                _mbmz_log.info(f"HTTP error {exc.code}")
            elif exc.code in (401,):
                raise _mbmz.AuthenticationError(cause=exc)
            else:
                # Any other HTTP status (e.g. 307 redirect loop) is a permanent failure.
                # The original code retried here — that can produce ~60 s hangs on redirect loops.
                raise _mbmz.ResponseError(cause=exc)
            last_exc = exc
        except _mbcompat.BadStatusLine as exc:
            _mbmz_log.info("bad status line")
            last_exc = exc
        except _mbcompat.HTTPException as exc:
            _mbmz_log.info(f"miscellaneous HTTP exception: {exc}")
            last_exc = exc
        except _mbcompat.URLError as exc:
            if isinstance(exc.reason, socket.error):
                code = exc.reason.errno
                if code == 104:  # "Connection reset by peer."
                    continue
            raise _mbmz.NetworkError(cause=exc)
        except TimeoutError as exc:
            _mbmz_log.info("socket timeout")
            last_exc = exc
        except OSError as exc:
            # socket.error is OSError in Python 3; handles both socket-level errors and
            # the original IOError branch from musicbrainzngs.  Code 104 = connection reset.
            if exc.errno == 104:
                continue
            raise _mbmz.NetworkError(cause=exc)
    raise _mbmz.NetworkError(f"retried {max_retries} times", last_exc)


_mbmz._safe_read = _patched_safe_read  # noqa: SLF001  # pylint: disable=protected-access

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
    work relations, recording-level relations, and disc IDs (so that each medium's ``discs`` list is populated for
    TOC-based medium selection).

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
            "url-rels",
            "series-rels",
            "isrcs",
            "discids",
        ],
    )
    return result


#: Relation types on recordings whose ``begin``/``end`` date range indicates the recording session.
_SESSION_REL_TYPES: frozenset[str] = frozenset(
    {"conductor", "performing orchestra", "balance", "engineer", "recording", "mix", "audio", "sound"}
)


def _extract_session_date(artist_relation_list: list[MBArtistRelation]) -> tuple[str, str]:
    """Extract the session begin and end dates from a recording's artist relations.

    The recording session date range is stored as the ``begin`` / ``end`` fields on artist-level
    relations such as ``"conductor"``, ``"performing orchestra"``, ``"balance"``, ``"engineer"``,
    etc.  All qualifying relations for a given session should carry the same dates.

    :param artist_relation_list: The recording's artist relations as parsed by musicbrainzngs.
    :returns: A ``(begin, end)`` tuple of ISO date strings.  ``begin`` is the minimum (earliest)
        begin date across all session-type relations with a non-empty begin.  ``end`` is the
        maximum (latest) end date across all session-type relations with a non-empty end.  Either
        component may be ``""`` when not present.
    """
    begins = [rel.begin for rel in artist_relation_list if rel.type in _SESSION_REL_TYPES and rel.begin]
    ends = [rel.end for rel in artist_relation_list if rel.type in _SESSION_REL_TYPES and rel.end]
    return (min(begins) if begins else "", max(ends) if ends else "")


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
        includes=["artists", "work-rels", "artist-rels", "work-level-rels", "isrcs"],
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


def _infer_mime(data: bytes) -> str:
    """Infer the MIME type of ``data`` from its leading magic bytes.

    :param data: Raw image or document bytes.
    :returns: A MIME type string such as ``"image/jpeg"``, ``"image/png"``, ``"application/pdf"``,
        or ``"image/tiff"``.  Returns ``"image/jpeg"`` for unrecognised formats.
    """
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:4] in (b"II\x2a\x00", b"MM\x00\x2a"):
        return "image/tiff"
    return "image/jpeg"


#: Mapping from CAA type string to the CoverArt bucket name (field name on CoverArt).
#: Priority order matters — when an image has multiple types, the first matching bucket
#: in this dict determines the primary bucket and therefore the sidecar filename.
_CAA_TYPE_TO_BUCKET: dict[str, str] = {
    "Front": "front",
    "Back": "back",
    "Booklet": "booklet",
    "Medium": "medium",
    "Tray": "tray",
    "Obi": "obi",
    "Spine": "spine",
    "Track": "track",
    "Liner": "liner",
    "Sticker": "sticker",
    "Poster": "poster",
    "Matrix/Runout": "matrix",
    "Top": "top",
    "Bottom": "bottom",
    "Panel": "panel",
    "Watermark": "watermark",
    "Raw/Unedited": "raw",
    "Other": "other",
}


def _sidecar_filename(image_type: str, count: int, index: int, mime: str) -> str:
    """Return the suggested sidecar filename for a CAA image.

    Single items of a given type (``count == 1``) use no index suffix (e.g. ``"back.jpg"``).
    Multiple items use a 1-based index suffix (e.g. ``"booklet-1.pdf"``, ``"booklet-2.jpg"``).
    The extension is derived from the MIME type.

    :param image_type: A bucket name such as ``"front"``, ``"back"``, ``"booklet"``, ``"matrix"``, etc.
    :param count: Total number of images of this type (used to decide whether to add an index).
    :param index: 1-based position of this image within its type group.
    :param mime: MIME type string (e.g. ``"image/jpeg"``).
    :returns: A filename string such as ``"cover.jpg"``, ``"booklet-1.pdf"``, ``"matrix-1.jpg"``.
    """
    _ext_map: dict[str, str] = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "application/pdf": ".pdf",
        "image/tiff": ".tiff",
    }
    ext = _ext_map.get(mime, ".bin")
    _base_map = {
        "front": "cover",
        "back": "back",
        "booklet": "booklet",
        "medium": "medium",
        "tray": "tray",
        "obi": "obi",
        "spine": "spine",
        "track": "track",
        "liner": "liner",
        "sticker": "sticker",
        "poster": "poster",
        "matrix": "matrix",
        "top": "top",
        "bottom": "bottom",
        "panel": "panel",
        "watermark": "watermark",
        "raw": "raw",
        "other": "other",
        "unknown": "unknown",
    }
    base = _base_map.get(image_type, image_type)
    if count > 1:
        return f"{base}-{index}{ext}"
    return f"{base}{ext}"


def _fetch_rg_image(
    release_group_id: str,
    size: str,
    imgs: list[CoverImage],
    filename: str,
    url: str,
    cache_dir: Path | None,
) -> None:
    """Fetch one release-group front image (either 500 px or original) and append to ``imgs``.

    Checks the cache directory for a pre-downloaded copy before hitting the network.  Writes the
    fetched bytes back to the cache on a miss.  A 404 from the CAA is treated as "image not
    available" — a warning is logged and the function returns without appending anything.  This
    handles the known CAA data-integrity condition where the MB metadata still references an image
    that has since been deleted from object storage.

    :param release_group_id: MusicBrainz release-group MBID.
    :param size: Size string for ``mb.get_release_group_image_front`` (e.g. ``"500"``); ``""`` for original.
    :param imgs: List to append the resulting :class:`~music_annotator.models.CoverImage` to.
    :param filename: ``filename`` attribute to set on the image (empty string leaves it unset).
    :param url: Canonical CAA URL to record on the image.
    :param cache_dir: Cache directory, or ``None`` when caching is disabled.
    :raises mb.ResponseError: On any non-404 network or HTTP error from the CAA API.
    """
    key = _cover_art_cache_key(f"rg_{release_group_id}", size)
    data: bytes | None = None
    if cache_dir is not None:
        cached = cache_dir / f"{key}.bin"
        if cached.is_file():
            data = cached.read_bytes()
            log.debug("cover_art_cache_hit", key=key, bytes=len(data))
    if data is None:
        try:
            if size:
                raw = _mb_call(lambda: mb.get_release_group_image_front(release_group_id, size=size))
            else:
                raw = _mb_call(lambda: mb.get_release_group_image_front(release_group_id))
        except mb.ResponseError as exc:
            if "404" in str(exc):
                log.warning(
                    "cover_art_rg_image_not_found",
                    release_group_id=release_group_id,
                    size=size or "original",
                )
                return
            raise
        if raw:
            data = bytes(raw)
            if cache_dir is not None:
                cache_path = cache_dir / f"{key}.bin"
                cache_path.write_bytes(data)
                log.debug("cover_art_cache_written", key=key, bytes=len(data))
    if data is not None:
        img = CoverImage(data=data, mime=_infer_mime(data), url=url)
        if filename:
            img.filename = filename
        imgs.append(img)


def _cover_art_cache_dir() -> Path:
    """Return the cover art cache directory, creating it if necessary.

    Resolves ``$XDG_CACHE_HOME/music-annotator/cover-art/`` (falling back to
    ``~/.cache/music-annotator/cover-art/`` when ``XDG_CACHE_HOME`` is unset or empty).
    The directory is created with ``parents=True, exist_ok=True`` on every call.

    :returns: A :class:`~pathlib.Path` for the cache directory.
    """
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    cache = base / "music-annotator" / "cover-art"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _cover_art_cache_key(coverid: str, size: str) -> str:
    """Return the cache filename stem for a cover art image.

    The key encodes both the CAA image identifier and the requested size so that the 500 px
    thumbnail and the original resolution image are stored as separate cache entries.

    For release-specific images, callers prefix ``coverid`` with the release MBID
    (e.g. ``f"{rel_id}_{caa_image_id}"``), scoping the cache entry to that release and
    preventing stale hits when a CAA image ID is reused or referenced by multiple releases.
    For release-group fallback images, callers prefix with ``f"rg_{release_group_id}"``.
    MusicBrainz UUIDs contain only hex digits and hyphens (no underscores), and CAA image
    IDs are plain integers, so the underscore separator is unambiguous in both cases.

    :param coverid: The prefixed identifier string — either ``"<rel_id>_<caa_image_id>"``
        for release-specific images or ``"rg_<release_group_id>"`` for release-group fallback
        images.
    :param size: The size string passed to ``mb.get_image`` (e.g. ``"500"``), or ``""`` for original.
    :returns: A filename-safe string such as ``"<rel_id>_12345678_500"`` or ``"rg_<rg_id>_original"``.
    """
    return f"{coverid}_{size or 'original'}"


def fetch_cover_art(release_id: str, release_group_id: str = "", no_cache: bool = False) -> CoverArt:
    """Download all available cover art for a release from the Cover Art Archive.

    Strategy:

    1. Call ``mb.get_image_list(release_id)`` to obtain the full CAA image listing.
    2. Classify each image entry by its ``types`` list into one of: ``front``, ``back``, ``booklet``,
       or ``medium``.  Images with unrecognised types are skipped.
    3. For **front** images: fetch twice — 500 px (for ``CoverArt.front``, embedded in audio files)
       and original resolution (for ``CoverArt.front_full``, written as ``cover.jpg`` sidecar).
    4. For **back/booklet/medium** images: fetch original resolution only; set ``filename`` and ``url``
       on each :class:`~music_annotator.models.CoverImage` for sidecar writing and journal provenance.
    5. If the release has no CAA listing (HTTP 404) and ``release_group_id`` is provided, fall back to
       the release-group front image using the same two-fetch strategy.

    The ``url`` field on each image is the canonical CAA URL from the image listing's ``"image"`` key,
    which is stable and publicly accessible regardless of the Internet Archive redirect target.

    :param release_id: The MusicBrainz release MBID.
    :param release_group_id: The MusicBrainz release-group MBID used as a fallback when the release has
        no CAA listing.  Pass an empty string to skip the fallback.
    :param no_cache: When ``True``, bypass the on-disk image cache entirely — always fetch from the
        network and do not write new cache entries.  Defaults to ``False``.
    :returns: A :class:`~music_annotator.models.CoverArt` instance.
    :raises RuntimeError: If the image listing call fails with a non-404 HTTP error.
    :raises mb.ResponseError: If any individual image fetch fails with a non-404 HTTP error.
    """
    cache_dir: Path | None = None if no_cache else _cover_art_cache_dir()

    def _fetch_raw(rel_id: str, coverid: str | int, bucket: str, size: str = "") -> CoverImage | None:
        """Fetch a single image from the cache or network; return a :class:`~music_annotator.models.CoverImage` or ``None``.

        Checks ``$XDG_CACHE_HOME/music-annotator/cover-art/`` for a previously downloaded copy before
        making a network request.  On a cache miss the image is fetched and written to the cache
        directory.  When ``no_cache`` is ``True`` the cache directory is ``None`` and both read and
        write are skipped.  Returns ``None`` when the API returns empty bytes or a 404 (image was
        deleted from CAA after the listing was fetched — a known CAA data-integrity condition).
        Non-404 HTTP errors propagate to the caller.

        :param rel_id: MusicBrainz release MBID.
        :param coverid: CAA image identifier.
        :param bucket: The destination bucket name (e.g. ``"front"``, ``"back"``), logged with the fetch.
        :param size: Optional size string passed to ``mb.get_image`` (e.g. ``"500"``); omit for original.
        :raises mb.ResponseError: On any non-404 network or HTTP error from the CAA API.
        """
        key = _cover_art_cache_key(f"{rel_id}_{coverid}", size)
        if cache_dir is not None:
            cached = cache_dir / f"{key}.bin"
            if cached.is_file():
                data = cached.read_bytes()
                log.debug("cover_art_cache_hit", key=key, bytes=len(data))
                return CoverImage(data=data, mime=_infer_mime(data))
        try:
            if size:
                raw = _mb_call(lambda: mb.get_image(rel_id, coverid, size=size))
            else:
                raw = _mb_call(lambda: mb.get_image(rel_id, coverid))
        except mb.ResponseError as exc:
            if "404" in str(exc):
                log.warning("cover_art_image_not_found", coverid=str(coverid), bucket=bucket, size=size or "original")
                return None
            raise
        if not raw:
            return None
        data = bytes(raw)
        if cache_dir is not None:
            cache_path = cache_dir / f"{key}.bin"
            cache_path.write_bytes(data)
            log.debug("cover_art_cache_written", key=key, bytes=len(data))
        img = CoverImage(data=data, mime=_infer_mime(data))
        log.info(
            "cover_art_image_fetched",
            coverid=str(coverid),
            size=size or "original",
            mime=img.mime,
            bytes=len(data),
            bucket=bucket,
        )
        return img

    log.info("fetch_cover_art", release_id=release_id)

    # Step 1: obtain the image listing.
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
                raise RuntimeError(f"cover art listing failed for release {release_id!r}: {code[:80]}") from exc

    # Step 2: classify images by type (first pass — no fetching yet).
    # Each image may carry multiple types (e.g. ["Back", "Spine"]).  We route each image to
    # ALL matching buckets; the primary bucket (first match in _CAA_TYPE_TO_BUCKET priority
    # order) determines the sidecar filename — secondary buckets reuse the same filename so the
    # file is written once and multiple COVERART_*_FILES tags point to it.
    # "unknown" is a synthetic bucket (not a CAA type) for images with unrecognised type strings.
    _all_buckets: list[str] = list(_CAA_TYPE_TO_BUCKET.values()) + ["unknown"]
    _classified: dict[str, list[tuple[str, str]]] = {b: [] for b in _all_buckets}
    if has_release_listing:
        for entry in listing:
            types_raw = entry.get("types", [])
            if not isinstance(types_raw, list):
                continue
            types = [t for t in types_raw if isinstance(t, str)]
            coverid = str(entry.get("id", ""))
            if not coverid:
                continue
            # The "image" key in the listing is the canonical CAA URL for this image.
            caa_url = str(entry.get("image", ""))
            # Map each CAA type string to its bucket; collect all matched buckets.
            matched_buckets = [_CAA_TYPE_TO_BUCKET[t] for t in types if t in _CAA_TYPE_TO_BUCKET]
            if matched_buckets:
                for bkt in matched_buckets:
                    _classified[bkt].append((coverid, caa_url))
            else:
                # Type string not in _CAA_TYPE_TO_BUCKET — route to unknown bucket and warn.
                log.warning("cover_art_unknown_types", types=types, coverid=coverid)
                _classified["unknown"].append((coverid, caa_url))

    # Step 3 & 4: fetch images with correct sizing and assign filenames/URLs.
    imgs_front: list[CoverImage] = []
    imgs_front_full: list[CoverImage] = []

    # Accumulators for all non-front sidecar buckets, including the synthetic "unknown" bucket.
    imgs_sidecar: dict[str, list[CoverImage]] = {b: [] for b in _all_buckets if b != "front"}

    # Track which coverids have already been fetched so multi-type images are fetched only once.
    # Maps coverid -> the CoverImage already fetched (primary bucket's fetch result).
    _fetched_originals: dict[str, CoverImage | None] = {}

    # Front: fetch 500px for embedding, original for sidecar.
    front_count = len(_classified["front"])
    for idx, (coverid, caa_url) in enumerate(_classified["front"], start=1):
        img_500 = _fetch_raw(release_id, coverid, bucket="front", size="500")
        if img_500:
            img_500.url = caa_url
            imgs_front.append(img_500)
        img_orig = _fetch_raw(release_id, coverid, bucket="front")
        if img_orig:
            img_orig.filename = _sidecar_filename("front", front_count, idx, img_orig.mime)
            img_orig.url = caa_url
            imgs_front_full.append(img_orig)
            _fetched_originals[coverid] = img_orig

    # All non-front buckets: fetch original once per coverid, assign sidecar filenames.
    # For each bucket, determine the index *within that bucket* for naming purposes.
    # Multi-type images: primary bucket fetches and names; secondary buckets reuse the
    # same CoverImage (same filename) so the file is written once.
    non_front_buckets = [b for b in _all_buckets if b != "front"]

    # Build per-bucket index maps: coverid -> 1-based index within its bucket
    # (needed for sidecar_filename when count > 1).
    for bkt in non_front_buckets:
        entries = _classified[bkt]
        count = len(entries)
        # Determine the primary bucket for each coverid (the first bucket in priority order
        # whose _classified list contains this coverid).
        for idx, (coverid, caa_url) in enumerate(entries, start=1):
            # Check if we've already fetched this coverid (it appeared in a higher-priority bucket).
            if coverid in _fetched_originals:
                existing = _fetched_originals[coverid]
                if existing is not None:
                    # Reuse the same CoverImage — same filename, same data, written once.
                    imgs_sidecar[bkt].append(existing)
                continue
            # First time seeing this coverid — fetch it and assign filename from this bucket.
            img = _fetch_raw(release_id, coverid, bucket=bkt)
            if img:
                img.filename = _sidecar_filename(bkt, count, idx, img.mime)
                img.url = caa_url
                imgs_sidecar[bkt].append(img)
                _fetched_originals[coverid] = img
            else:
                _fetched_originals[coverid] = None

    # Step 5: release-group fallback (no listing available).
    if not has_release_listing and release_group_id:
        rg_url = f"https://coverartarchive.org/release-group/{release_group_id}/front"
        _fetch_rg_image(release_group_id, "500", imgs_front, "", rg_url, cache_dir)
        _fetch_rg_image(release_group_id, "", imgs_front_full, "cover.jpg", rg_url, cache_dir)

    result_art = CoverArt(
        front=imgs_front,
        front_full=imgs_front_full,
        **{b: imgs_sidecar[b] for b in imgs_sidecar},
    )
    if result_art.available:
        counts = {b: len(imgs_sidecar[b]) for b in imgs_sidecar if imgs_sidecar[b]}
        log.info("cover_art_fetched", front=len(imgs_front), front_full=len(imgs_front_full), **counts)
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
        includes=["artist-rels", "work-rels", "url-rels", "label-rels", "place-rels", "tags", "aliases", "annotation"],
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
