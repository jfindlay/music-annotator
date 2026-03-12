"""Release discovery helpers for music-annotator.

Provides :func:`discover`, the interactive multi-directory workflow that searches MusicBrainz for
matching releases and then calls :func:`run` to copy and tag each directory.  Also exposes the
:class:`DiscoverUI` Protocol for dependency injection and :class:`TerminalDiscoverUI` as the
default concrete implementation.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import musicbrainzngs as mb
import structlog
import yaml

from music_annotator._console import _console
from music_annotator._mb_api import _mb_retry, init_mb
from music_annotator._pipeline import CollisionPolicy, run
from music_annotator._pipeline_io import find_source_files
from music_annotator.models import JSON, MBReleaseCandidate

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

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


class DiscoverUI(Protocol):
    """Protocol for the interactive UI layer used by :func:`discover`.

    Separates the user-interaction concerns (printing candidates, prompting for a choice,
    prompting for source-directory deletion) from the business logic, enabling test doubles.
    """

    def choose_release(self, src_dir: Path, candidates: list[MBReleaseCandidate]) -> str | None:
        """Present ``candidates`` and return a selected release MBID, or ``None`` to skip.

        :param src_dir: The source directory being processed.
        :param candidates: Ordered list of :class:`~music_annotator.models.MBReleaseCandidate` to present.
        :returns: A release MBID string, or ``None`` when the user chooses to skip.
        """

    def confirm_delete(self, src_dir: Path) -> bool:
        """Ask the user whether to delete ``src_dir`` after a successful copy.

        :param src_dir: The source directory that was just copied.
        :returns: ``True`` if the user confirms deletion.
        """


class TerminalDiscoverUI:
    """Concrete :class:`DiscoverUI` implementation that interacts via stdin/stdout.

    Uses the module-level :data:`~music_annotator._console._console` rich console for formatted
    output and ``input()`` for user prompts.
    """

    def choose_release(self, src_dir: Path, candidates: list[MBReleaseCandidate]) -> str | None:
        """Print candidate list, prompt for a choice, and return the selected MBID or ``None``.

        :param src_dir: The source directory being processed (used in the display header).
        :param candidates: Ordered list of candidates to display.
        :returns: A release MBID string, or ``None`` when the user enters ``s`` / ``skip`` / empty.
        """
        _console.print(f"\n  [bold]Candidates for[/] [bold cyan]{src_dir.name}[/]:")
        for i, candidate in enumerate(candidates, 1):
            _console.print(_format_candidate(i, candidate))
            _console.print()

        _console.print(f"  [bold]Enter a number (1–{len(candidates)}), a raw MBID, or 's' to skip:[/]")
        _console.print("  [bold]>[/] ", end="")
        choice = input("").strip()

        if choice.lower() in {"s", "skip", ""}:
            return None

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx].release_id
            _console.print(f"  [yellow]Invalid selection '{choice}', skipping.[/]")
            return None

        # Treat as raw MBID
        return choice

    def confirm_delete(self, src_dir: Path) -> bool:
        """Ask whether to delete ``src_dir`` and return ``True`` if confirmed.

        :param src_dir: The source directory to potentially delete.
        :returns: ``True`` when the user answers ``y`` or ``yes``.
        """
        _console.print(f"\n  [bold]Delete original directory[/] [bold red]{src_dir}[/][bold]?[/] [dim](y/n)[/] ", end="")
        return input("").strip().lower() in {"y", "yes"}


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
    ui: DiscoverUI | None = None,
) -> None:
    """Search MusicBrainz for releases matching each source directory, prompt for confirmation, then apply tags.

    For each directory in ``src_dirs`` the function:

    1. Searches MB using :func:`search_releases_by_dir` and presents a numbered candidate list.
    2. Prompts the user to enter a candidate number, a raw MBID, or ``s`` to skip.
    3. If a valid selection is made, calls :func:`run` to copy and tag that directory.
    4. After a successful copy (unless ``dry_run`` is set), prompts the user to delete the original source directory.

    :param src_dirs: List of source directories to process in order.
    :param dest_root: Root destination directory for the annotated music library.
    :param user_agent: MusicBrainz user-agent string.
    :param dry_run: When ``True``, pass through to :func:`run` without writing files; the delete prompt is suppressed.
    :param fetch_rels: When ``False``, skip per-recording relation lookups in :func:`run`.
    :param limit: Maximum number of search candidates to display per directory.
    :param ui: A :class:`DiscoverUI` instance for user interaction.  Defaults to :class:`TerminalDiscoverUI`.
    """
    if ui is None:
        ui = TerminalDiscoverUI()
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

        release_id = ui.choose_release(src_dir, candidates)
        if release_id is None:
            log.info("discover_skipped", path=str(src_dir))
            continue

        log.info("discover_selected", release_id=release_id, src_dir=str(src_dir))
        try:
            run(
                release_id=release_id,
                src_dir=src_dir,
                dest_root=dest_root,
                user_agent=user_agent,
                dry_run=dry_run,
                fetch_rels=fetch_rels,
                collision_policy=CollisionPolicy.ASK,
            )
        except (ValueError, mb.WebServiceError, RuntimeError, OSError) as exc:
            log.error("discover_run_error", release_id=release_id, error=str(exc), exc_info=True)
            continue

        if not dry_run:
            if ui.confirm_delete(src_dir):
                shutil.rmtree(src_dir)
                log.info("discover_deleted_src", path=str(src_dir))
            else:
                log.info("discover_kept_src", path=str(src_dir))
