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
from rich.markup import escape as _markup_escape

from music_annotator._console import _console
from music_annotator._mb_api import _fetch_acoustid_lookup_raw, _mb_data_classify, fetch_release, init_mb
from music_annotator._net import NetPolicy, retrieve
from music_annotator._pipeline import CollisionPolicy, run
from music_annotator._pipeline_io import (
    _DISC_INFO_FILENAME,
    JOURNAL_FILENAME,
    AudioCompareResult,
    _corroborate_candidate_medium,
    _find_whipper_log,
    _load_disc_info_yaml,
    _preferred_disc_record,
    _read_duration_ms,
    _read_isrc_tag,
    _run_fpcalc,
    find_source_files,
    parse_disc_toc,
    parse_whipper_log,
    read_journal,
)
from music_annotator._tags import _NAME_MAX
from music_annotator.models import (
    JSON,
    AccurateRipSummary,
    AccurateRipTrack,
    DirHint,
    MBMedium,
    MBReleaseCandidate,
    TransactionLog,
)

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

    def confirm_disc(
        self,
        mediums: list[MBMedium],
        proposed: MBMedium,
        dtitle: str,
        release_url: str,
    ) -> MBMedium | None:
        """Confirm or override a disc selected by the FreeDB title heuristic.

        Shown when MusicBrainz has no disc IDs for a release and medium selection fell back to
        token-overlap scoring.  The user may accept the proposed disc, choose a different one by
        number, or abort the run.

        :param mediums: All mediums for the release (to show as alternatives).
        :param proposed: The medium selected by the title-match heuristic.
        :param dtitle: FreeDB disc title used for the match, displayed for context.
        :param release_url: MusicBrainz release URL, displayed so the user can inspect the release.
        :returns: The confirmed or user-chosen :class:`~music_annotator.models.MBMedium`, or ``None``
            to abort the run for this directory.
        """

    def confirm_shortened_name(self, original: str, proposed: str) -> str | None:
        """Confirm or override a path component that exceeds :data:`~music_annotator._tags._NAME_MAX` bytes.

        Called once per unique too-long component before any files are written.  The user may
        accept the proposed shortened name, type a custom replacement, or abort the run.

        :param original: The full computed path component that exceeds the byte limit.
        :param proposed: The auto-shortened component produced by
            :func:`~music_annotator._tags._proposed_short`.
        :returns: The confirmed replacement string, or ``None`` to abort.
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
        _console.print(f"\n  [bold]Candidates for[/] [bold cyan]{_markup_escape(src_dir.name)}[/]:")
        for i, candidate in enumerate(candidates, 1):
            _console.print(_format_candidate(i, candidate))
            _console.print()

        _console.print(f"  [dim]Enter a number (1–{len(candidates)}), a raw MBID, or 's' to skip[/]")
        _console.print("\n[bold cyan]>[/] ", end="")
        choice = input("").strip()

        if choice.lower() in {"s", "skip", ""}:
            return None

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx].release_id
            _console.print(f"  [bold yellow]Invalid selection '{choice}', skipping.[/]")
            return None

        # Treat as raw MBID
        return choice

    def confirm_disc(
        self,
        mediums: list[MBMedium],
        proposed: MBMedium,
        dtitle: str,
        release_url: str,
    ) -> MBMedium | None:
        """Display the title-match result and prompt the user to confirm, override, or abort.

        Prints the FreeDB title, the proposed disc with its first track title, and a numbered list
        of all available mediums as alternatives.  Accepts ``y`` / ``yes`` to confirm, ``n`` / ``no``
        / ``a`` / ``abort`` to abort, or a disc number ``1``–``N`` to choose a different medium.

        :param mediums: All mediums for the release.
        :param proposed: The medium selected by the title-match heuristic.
        :param dtitle: FreeDB disc title used for the match.
        :param release_url: MusicBrainz release URL.
        :returns: The confirmed or user-chosen :class:`~music_annotator.models.MBMedium`, or ``None``
            to abort.
        """
        first_track = proposed.track_list[0].recording.title if proposed.track_list else "(no tracks)"
        _console.print(
            "\n[bold yellow]WARNING:[/] [yellow]No disc IDs in MusicBrainz — disc selected by FreeDB title match.[/]"
        )
        _console.print(f"  [dim]FreeDB title :[/] {dtitle}")
        _console.print(f"  [dim]Proposed disc:[/] [bold]{proposed.position}[/] — {first_track}")
        _console.print(f"  [dim]Release URL  :[/] {release_url}")
        _console.print("\n  [dim]Available discs:[/]")
        for m in mediums:
            ft = m.track_list[0].recording.title if m.track_list else "(no tracks)"
            marker = " [bold cyan]←[/]" if m is proposed else ""
            _console.print(f"    [{m.position}] disc {m.position}: {ft}{marker}")
        _console.print(f"\n  [dim]Enter [bold]y[/] to accept, [bold]n[/] to abort, or a disc number (1–{len(mediums)}):[/]")
        while True:
            _console.print("\n[bold cyan]>[/] ", end="")
            choice = input("").strip().lower()
            match choice:
                case "y" | "yes":
                    return proposed
                case "n" | "no" | "a" | "abort":
                    return None
                case s if s.isdigit():
                    pos = int(s)
                    hits = [m for m in mediums if m.position == pos]
                    if hits:
                        return hits[0]
                    _console.print(f"  [bold yellow]No disc at position {pos}.[/]")
                case _:
                    _console.print("  [bold yellow]Please enter y, n, or a disc number.[/]")

    def confirm_shortened_name(self, original: str, proposed: str) -> str | None:
        """Display the too-long component, show the proposed shortened name, and prompt for confirmation.

        Accepts ``y`` / ``yes`` to accept ``proposed``, ``q`` / ``quit`` / ``a`` / ``abort`` to abort,
        or any other non-empty input as a custom replacement (re-prompts if the custom value still
        exceeds :data:`~music_annotator._tags._NAME_MAX` bytes after :func:`~music_annotator._tags.safe_name`
        sanitisation).

        :param original: The full computed path component that exceeds the byte limit.
        :param proposed: The auto-shortened component produced by
            :func:`~music_annotator._tags._proposed_short`.
        :returns: The confirmed replacement string, or ``None`` to abort.
        """
        from music_annotator._tags import safe_name  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        orig_bytes = len(original.encode("utf-8"))
        prop_bytes = len(proposed.encode("utf-8"))
        _console.print(f"\n[bold red]WARNING:[/] [red]Path component exceeds {_NAME_MAX} bytes ({orig_bytes} bytes):[/]")
        _console.print(f"  [dim]Original  ({orig_bytes:3d} B):[/] {original}")
        _console.print(f"  [dim]Proposed  ({prop_bytes:3d} B):[/] [bold]{proposed}[/]")
        _console.print("\n  [dim]Enter [bold]y[/] to accept the proposed name, [bold]q[/] to abort,")
        _console.print(f"  [dim]or type a custom replacement (must be ≤ {_NAME_MAX} bytes):[/]")
        while True:
            _console.print("\n[bold cyan]>[/] ", end="")
            choice = input("").strip()
            match choice.lower():
                case "y" | "yes":
                    return proposed
                case "q" | "quit" | "a" | "abort":
                    return None
                case _ if choice:
                    sanitised = safe_name(choice)
                    if len(sanitised.encode("utf-8")) <= _NAME_MAX:
                        return sanitised
                    _console.print(
                        f"  [bold yellow]'{sanitised}' is {len(sanitised.encode('utf-8'))} bytes — still too long.  "
                        f"Please try again.[/]"
                    )
                case _:
                    _console.print("  [bold yellow]Please enter y, q, or a custom name.[/]")

    def confirm_delete(self, src_dir: Path) -> bool:
        """Ask whether to delete ``src_dir`` and return ``True`` if confirmed.

        :param src_dir: The source directory to potentially delete.
        :returns: ``True`` when the user answers ``y`` or ``yes``.
        """
        _console.print(
            f"\n[bold red]Delete original directory[/] [red]{_markup_escape(str(src_dir))}[/][bold red]?[/] [dim](y/n)[/]"
        )
        _console.print("\n[bold cyan]>[/] ", end="")
        return input("").strip().lower() in {"y", "yes"}


def parse_disc_info_yaml(src_dir: Path) -> DirHint | None:  # pylint: disable=too-many-return-statements
    """Extract a :class:`~music_annotator.models.DirHint` from a FreeDB ``00 - disc info.yaml`` file.

    The file contains a ``record`` list of FreeDB entries for the disc.  Each entry has a ``track_info`` dict with a ``DTITLE``
    key whose value is ``"artist / title"`` — the `` / `` separator is the FreeDB standard.  When multiple records are present
    the one marked ``preferred: true`` is used; if none is marked preferred the first record is used.

    :param src_dir: Directory that may contain a ``00 - disc info.yaml`` file.
    :returns: A :class:`~music_annotator.models.DirHint` with ``query`` and ``artist`` populated if a usable ``DTITLE`` is
        found, or ``None`` if the file is absent, the record list is empty, or ``DTITLE`` is missing / blank.
    :raises yaml.YAMLError: Propagated if the file exists but cannot be parsed.
    """
    data = _load_disc_info_yaml(src_dir)
    if data is None:
        return None

    records: object = data.get("record")
    if not isinstance(records, list) or not records:
        return None

    preferred = _preferred_disc_record(records)
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
        return DirHint(query=title.strip(), artist=artist.strip())
    return DirHint(query=dtitle, artist="")


def _toc_lookup_mb_releases(toc_string: str, limit: int) -> list[dict[str, object]]:
    """Query MusicBrainz for releases matching a CD TOC string.

    Calls ``mb.get_releases_by_discid`` with ``toc=toc_string`` so that the MB server performs a fuzzy TOC match even when the
    exact disc ID is not in the database.  The call uses a sentinel disc ID (``"intentionally-invalid-id"``) that will never
    match, ensuring the server always falls through to the fuzzy TOC path and returns a ``"release-list"`` dict.  The result is
    routed through :func:`~music_annotator._net.retrieve` with :func:`~music_annotator._mb_api._mb_data_classify` for retry,
    polite delay, and structured 404 handling.

    Response shapes handled:

    * ``{"disc": {"release-list": [...]}}`` — exact disc-ID match (rare).
    * ``{"release-list": [...], "release-count": N}`` — fuzzy TOC match (typical).
    * :attr:`~music_annotator._net.RetryDecision.NO_DATA` (404) — no matches; return ``[]``.

    :param toc_string: A TOC string in the form ``"1 <num_tracks> <leadout_frame> <offset_1> … <offset_N>"``.
    :param limit: Maximum number of results to slice from the response list.
    :returns: A list of raw release dicts (possibly empty).
    """

    def _call() -> dict[str, object]:
        return mb.get_releases_by_discid(  # type: ignore[no-any-return]
            "intentionally-invalid-id",
            toc=toc_string,
            includes=["artist-credits", "labels"],
        )

    policy = NetPolicy(classify=_mb_data_classify, event="mb_discid", log_fields={"toc_string": toc_string})
    raw = retrieve(_call, policy)
    if raw is None:
        # NO_DATA: 404 — no releases match this TOC (authoritative no-data from MB).
        return []
    response: dict[str, object] = raw

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


def parse_dir_hint(src_dir: Path) -> DirHint:
    """Extract a :class:`~music_annotator.models.DirHint` from a source directory name and its track filenames.

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
    :returns: A :class:`~music_annotator.models.DirHint` with the cleaned query and ``artist=""`` because the naming convention
        does not reliably distinguish artist from title.
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

    return DirHint(query=query, artist="")


def is_whipper_dir(src_dir: Path) -> bool:
    """Return ``True`` when ``src_dir`` is recognised as a whipper rip (C-WHIP strong signatures).

    A directory is a whipper rip when **either** of the two strong signatures is present:

    1. A ``*.log`` file whose last non-empty line matches ``SHA-256 hash: <UPPERHEX>`` (the
       whipper native-logger self-attesting hash appended by ``WhipperLogger.logRip``).  Detection
       delegates to :func:`~music_annotator._pipeline_io._find_whipper_log`.
    2. A ``00 - disc info.yaml`` file (``_DISC_INFO_FILENAME``) is present.

    The ``.0x…`` freedb-CRC dir-name suffix is a weak corroborating signal only — it is never
    sufficient alone to establish a whipper rip (C-WHIP).

    :param src_dir: Directory to inspect.
    :returns: ``True`` when at least one strong signature is present.
    """
    # Strong signature (2): 00 - disc info.yaml present.
    if (src_dir / _DISC_INFO_FILENAME).is_file():
        return True

    # Strong signature (1): *.log file with trailing SHA-256 hash line.
    # Delegates to _find_whipper_log to avoid duplicating the detection logic.
    return _find_whipper_log(src_dir) is not None


def is_download_dir(src_dir: Path) -> bool:
    """Return ``True`` when ``src_dir`` is recognised as a generic ISRC-bearing download (C-DL).

    Matches any ISRC-bearing download dir with no competing rip-provenance signature.

    A directory is a generic download when **both** conditions hold:

    1. At least one audio file yields a non-empty ISRC via :func:`~music_annotator._pipeline_io._read_isrc_tag`.
    2. No competing strong rip-provenance signature is present — no whipper native log
       (:func:`~music_annotator._pipeline_io._find_whipper_log` returns ``None``) and no
       ``00 - disc info.yaml`` (:data:`~music_annotator._pipeline_io._DISC_INFO_FILENAME`).  The
       ``00 - disc info.yaml`` check subsumes the "no resolvable TOC" condition because
       :func:`~music_annotator._pipeline_io.parse_disc_toc` reads exclusively from that file.

    Whipper recognition takes precedence (C-WHIP mutual exclusion): callers must check
    :func:`is_whipper_dir` first and skip this function when whipper is recognised.

    :param src_dir: Directory to inspect.
    :returns: ``True`` when the download heuristic matches; ``False`` otherwise.
    """
    # Condition 2: reject if any strong rip-provenance signature is present.
    # The disc info yaml check subsumes the "no resolvable TOC" condition because parse_disc_toc
    # reads exclusively from 00 - disc info.yaml.
    if (src_dir / _DISC_INFO_FILENAME).is_file():
        return False
    if _find_whipper_log(src_dir) is not None:
        return False

    # Condition 1: at least one audio file must carry a non-empty ISRC tag.
    return any(_read_isrc_tag(f) for f in find_source_files(src_dir))


def _parse_whipper_ar(src_dir: Path) -> tuple[AccurateRipSummary, dict[int, AccurateRipTrack]]:
    """Parse the whipper log in ``src_dir`` and return the AccurateRip summary and per-track data.

    Wraps :func:`~music_annotator._pipeline_io.parse_whipper_log` and returns empty defaults when
    no whipper log is found (e.g. when only strong signature (2) is present and no log exists).

    :param src_dir: Directory containing the whipper rip.
    :returns: A ``(AccurateRipSummary, dict[int, AccurateRipTrack])`` tuple; both are empty/default
        when no whipper native log is found.
    """
    try:
        return parse_whipper_log(src_dir)
    except FileNotFoundError:
        return AccurateRipSummary(), {}


def _search_mb_releases(query: str, tracks: int, limit: int) -> dict[str, JSON]:
    """Call ``mb.search_releases`` and return the raw response dict.

    Routes through :func:`~music_annotator._net.retrieve` with :func:`~music_annotator._mb_api._mb_data_classify` for retry,
    polite delay, and structured 404 handling.  On :attr:`~music_annotator._net.RetryDecision.NO_DATA` (authoritative 404),
    returns ``{}`` so the existing ``raw.get("release-list", [])`` in :func:`search_releases_by_dir` yields no candidates.

    :param query: Lucene query string for the ``release`` field.
    :param tracks: Expected total track count; added as a ``tracks`` field constraint when non-zero.
    :param limit: Maximum number of results to return.
    :returns: Raw ``musicbrainzngs`` response dict containing a ``"release-list"`` key, or ``{}`` on authoritative no-data.
    """

    def _call() -> dict[str, JSON]:
        if tracks:
            return mb.search_releases(query, limit=limit, tracks=tracks)  # type: ignore[no-any-return]
        return mb.search_releases(query, limit=limit)  # type: ignore[no-any-return]

    policy = NetPolicy(classify=_mb_data_classify, event="mb_search", log_fields={"query": query})
    raw = retrieve(_call, policy)
    # NO_DATA: 404 — authoritative no-data; map to {} so release-list lookup yields no candidates.
    return raw if raw is not None else {}


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
            # MB search responses shape each medium as track-count: N alongside track-list: []
            # (present but empty).  Use track-list length only when the list is non-empty;
            # otherwise fall back to track-count (which is always correct in search results).
            if isinstance(tl, list) and tl:
                total_tracks += len(tl)
            else:
                # Covers both the absent-list case (TOC responses) and the empty-list case
                # (search responses where track-list: [] accompanies track-count: N).
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
        query = hint.query
        log.debug("disc_info_yaml_hint", query=query, src_dir=str(src_dir))
    else:
        # --- Priority 3: directory name text search ---
        query = parse_dir_hint(src_dir).query
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


def _build_journal_release_ids(journal: TransactionLog) -> set[str]:
    """Extract the set of all release MBIDs that have been applied to tagged files.

    Reads only ``action == "tagged"`` entries from the journal.  Other actions (``"skipped"``,
    ``"dry_run"``, ``"downloaded"``, ``"sidecar"``) are excluded because only ``"tagged"`` entries
    represent a confirmed, verified copy-and-tag operation.

    :param journal: The :class:`~music_annotator.models.TransactionLog` to scan.
    :returns: A set of release MBID strings (deduplicated).
    """
    return {e.release_id for e in journal.entries if e.action == "tagged"}


def _enrich_candidates_from_journal(
    candidates: list[MBReleaseCandidate],
    journal_ids: set[str],
) -> list[MBReleaseCandidate]:
    """Flag and boost any MB candidate whose release MBID appears in the journal.

    For each candidate whose ``release_id`` is in ``journal_ids``, return a copy with
    ``from_journal=True`` and ``score`` raised to at least 101.  Candidates with no journal match
    are returned unchanged.  The result list is re-sorted by score descending so that journal-backed
    candidates float to the top, where the user will see them first.

    Score 101 is chosen to exceed the maximum MB relevance score (100), ensuring that any
    journal-confirmed candidate ranks above every purely organic search result.

    :param candidates: Ordered list of :class:`~music_annotator.models.MBReleaseCandidate` from
        :func:`search_releases_by_dir`.
    :param journal_ids: Set of release MBIDs known to the journal (from
        :func:`_build_journal_release_ids`).
    :returns: A new list of candidates sorted by score descending, with journal hits boosted.
    """
    enriched: list[MBReleaseCandidate] = []
    for candidate in candidates:
        if candidate.release_id in journal_ids:
            enriched.append(candidate.model_copy(update={"from_journal": True, "score": max(candidate.score, 101)}))
        else:
            enriched.append(candidate)
    enriched.sort(key=lambda c: c.score, reverse=True)
    return enriched


def _enrich_candidates_with_sequence_corroboration(
    source_paths: list[Path],
    candidates: list[MBReleaseCandidate],
    medium_track_ids_by_release: dict[str, list[str]],
) -> list[MBReleaseCandidate]:
    """Adjust candidate scores using medium-sequence corroboration of embedded recording-ID tags.

    For each candidate whose release MBID appears in ``medium_track_ids_by_release``, calls
    :func:`~music_annotator._pipeline_io._corroborate_candidate_medium` with the source files and
    the candidate medium's ordered recording IDs.  The corroboration result adjusts the candidate's
    score:

    * ``match=True`` (sequence confirmed) → score boosted by 10 points.
    * ``match=False`` (sequence contradicted) → score penalised by 20 points (floored at 0).
    * ``match=None`` (inconclusive) → score unchanged.

    Candidates whose release MBID is absent from ``medium_track_ids_by_release`` are returned
    unchanged (the full release data has not been fetched for them).

    The result list is re-sorted by score descending so that corroboration-boosted candidates
    float to the top.

    .. note::
        ``medium_track_ids_by_release`` is populated only when the caller has already fetched the
        full release data (e.g. from a prior session's journal or a pre-fetch step).  In the
        typical first-run case the dict is empty and this function is a no-op.  The cross-medium
        generalisation (spanning multiple media) is deferred until the multi-medium substrate lands.

    :param source_paths: Ordered list of source audio file paths, one per track.
    :param candidates: Ordered list of :class:`~music_annotator.models.MBReleaseCandidate` to enrich.
    :param medium_track_ids_by_release: Mapping of release MBID → ordered list of recording MBIDs
        on the candidate medium.  Populated by callers that have fetched the full release data.
    :returns: A new list of candidates sorted by score descending, with sequence-corroborated
        candidates adjusted.
    """
    enriched: list[MBReleaseCandidate] = []
    for candidate in candidates:
        track_ids = medium_track_ids_by_release.get(candidate.release_id)
        if track_ids is None:
            enriched.append(candidate)
            continue
        result: AudioCompareResult = _corroborate_candidate_medium(source_paths, track_ids)
        match result.match:
            case True:
                new_score = candidate.score + 10
                enriched.append(candidate.model_copy(update={"score": new_score}))
            case False:
                new_score = max(candidate.score - 20, 0)
                enriched.append(candidate.model_copy(update={"score": new_score}))
            case None:
                enriched.append(candidate)
            case _:  # pragma: no cover
                enriched.append(candidate)
    enriched.sort(key=lambda c: c.score, reverse=True)
    return enriched


def _enrich_candidates_with_acoustid_seed(
    source_files: list[Path],
    candidates: list[MBReleaseCandidate],
    acoustid_key: str,
) -> list[MBReleaseCandidate]:
    """Boost candidate scores when AcoustID fingerprint lookup confirms a track recording MBID.

    For each source file, computes a Chromaprint fingerprint via :func:`~music_annotator._pipeline_io._run_fpcalc`
    and reads the audio duration via :func:`~music_annotator._pipeline_io._read_duration_ms`, then calls
    :func:`~music_annotator._mb_api._fetch_acoustid_lookup_raw` to retrieve the ordered list of recording MBIDs
    that AcoustID associates with that fingerprint.  The union of all recording MBIDs across all source files
    is collected.

    For each candidate, the full release is fetched via :func:`~music_annotator._mb_api.fetch_release` to
    obtain the medium's track recording IDs.  Any candidate whose medium contains at least one recording ID
    that appears in the AcoustID results has its score boosted by 10 points (the same convention as
    :func:`_enrich_candidates_with_sequence_corroboration`).  Candidates for which the release fetch fails
    are returned unchanged.

    Returns ``candidates`` unchanged (no network calls, no score changes) when ``acoustid_key == ""``.

    The result list is re-sorted by score descending so that AcoustID-confirmed candidates float to the top.

    :param source_files: List of source audio file paths to fingerprint.
    :param candidates: Ordered list of :class:`~music_annotator.models.MBReleaseCandidate` to enrich.
    :param acoustid_key: AcoustID application API key.  When empty, the function is a no-op.
    :returns: A new list of candidates sorted by score descending, with AcoustID-confirmed candidates
        boosted by 10 points.
    """
    if not acoustid_key:
        return candidates

    # Collect all recording MBIDs returned by AcoustID for any source file.
    acoustid_recording_ids: set[str] = set()
    for path in source_files:
        fp = _run_fpcalc(path)
        dur_ms = _read_duration_ms(path)
        dur_s = dur_ms // 1000
        mbids, _ = _fetch_acoustid_lookup_raw(fp, dur_s, acoustid_key)
        acoustid_recording_ids.update(mbids)

    if not acoustid_recording_ids:
        return candidates

    enriched: list[MBReleaseCandidate] = []
    for candidate in candidates:
        # Fetch the full release to obtain track recording IDs for the candidate's medium.
        try:
            release = fetch_release(candidate.release_id)
        except Exception:  # noqa: BLE001 — fetch failure: leave score unchanged
            enriched.append(candidate)
            continue
        # Collect all recording IDs across all mediums of the release.
        release_recording_ids: set[str] = {track.recording.id for medium in release.medium_list for track in medium.track_list}
        if release_recording_ids & acoustid_recording_ids:
            new_score = candidate.score + 10
            enriched.append(candidate.model_copy(update={"score": new_score}))
        else:
            enriched.append(candidate)
    enriched.sort(key=lambda c: c.score, reverse=True)
    return enriched


def _format_candidate(index: int, candidate: MBReleaseCandidate) -> str:
    """Format a single :class:`~music_annotator.models.MBReleaseCandidate` as a human-readable numbered entry.

    Journal-confirmed candidates (``candidate.from_journal is True``) render as a compact two-line
    block showing only the MBID and URL — the full metadata fields are omitted because they describe
    a previously-applied release already known to the user.  All other candidates use the full
    seven-line display.

    :param index: 1-based display index.
    :param candidate: The candidate to format.
    :returns: A multi-line string ready to print to stdout.
    """
    if candidate.from_journal:
        lines = [
            f"  [bold yellow]\\[{index}][/] [bold green][journal match][/]  [dim]score={candidate.score}[/]",
            f"  [dim]     mbid   :[/] {candidate.release_id}",
            f"  [dim]     url    :[/] [dim cyan]{candidate.mb_url}[/]",
        ]
        return "\n".join(lines)

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


def prompt_delete_src(src_dir: Path, ui: DiscoverUI | None = None) -> None:
    """Prompt the user to confirm deletion of ``src_dir`` and delete it if confirmed.

    Used by the ``apply`` CLI subcommand after a successful copy when ``--delete`` is supplied, and
    internally by :func:`discover` when ``delete=True``.  Logs the outcome in both cases.

    :param src_dir: The source directory to potentially delete.
    :param ui: A :class:`DiscoverUI` instance for interaction.  Defaults to :class:`TerminalDiscoverUI`.
    """
    if ui is None:
        ui = TerminalDiscoverUI()
    if ui.confirm_delete(src_dir):
        shutil.rmtree(src_dir)
        log.info("deleted_src", path=str(src_dir))
    else:
        log.info("kept_src", path=str(src_dir))


def prune_sources(
    src_dir: Path,
    dest_root: Path,
    yes: bool = False,
    ui: DiscoverUI | None = None,
) -> None:
    """Check journal entries for ``src_dir`` and, after exact presence validation, delete it.

    Reads the transaction journal at ``dest_root / JOURNAL_FILENAME`` and filters to entries with
    ``action == "tagged"`` whose ``source`` path is under ``src_dir``.  Performs exact presence
    checks on both the source and destination sides before offering the user a deletion prompt.

    Source-side checks (``"tagged"`` entries only):

    - If ``src_dir`` does not exist: log at INFO level (already deleted) and return.
    - If ``src_dir`` is not a directory: log error and return.
    - If no ``"tagged"`` journal entries are found for ``src_dir``: log warning and return.
    - The set of audio files in ``src_dir`` (via :func:`find_source_files`, which excludes disc-TOC
      and disc-info files) must exactly match the set of source paths in the ``"tagged"`` entries.
      Any discrepancy is logged as an error and the function returns without deleting.

    Destination-side checks (``"tagged"`` and ``"sidecar"`` entries):

    - For each matched journal entry, the full destination file path must exist on disk.  Any missing
      destination file is logged as an error and the function returns without deleting.

    If all checks pass, a summary of the source directory and its destination paths is printed, and
    the user is asked to confirm deletion (or it is performed immediately if ``yes=True``).

    :param src_dir: The source directory to inspect and potentially delete.
    :param dest_root: Root destination directory; the journal is read from here.
    :param yes: When ``True``, skip the confirmation prompt and delete immediately.
    :param ui: A :class:`DiscoverUI` instance for interaction.  Defaults to :class:`TerminalDiscoverUI`.
    """
    if ui is None:
        ui = TerminalDiscoverUI()

    # Step 1 — source directory existence
    if not src_dir.exists():
        log.info("prune_src_already_deleted", path=str(src_dir))
        return
    if not src_dir.is_dir():
        log.error("prune_src_not_a_directory", path=str(src_dir))
        return

    # Step 2 — journal lookup
    journal = read_journal(dest_root / JOURNAL_FILENAME)
    src_prefix = str(src_dir) + "/"
    tagged = [e for e in journal.entries if e.action == "tagged" and e.source.startswith(src_prefix)]
    sidecars = [e for e in journal.entries if e.action == "sidecar" and e.source.startswith(src_prefix)]
    if not tagged:
        log.warning("prune_no_journal_entries", src_dir=str(src_dir))
        return

    # Step 3 — source-side exact presence check (audio files only; sidecars are excluded by find_source_files)
    expected_src = {Path(e.source) for e in tagged}
    actual_src = set(find_source_files(src_dir))
    missing_src = expected_src - actual_src
    extra_src = actual_src - expected_src
    if missing_src or extra_src:
        for p in sorted(missing_src):
            log.error("prune_src_file_missing", path=str(p))
        for p in sorted(extra_src):
            log.error("prune_src_unexpected_file", path=str(p))
        return

    # Step 4 — destination-side exact presence check for both tagged audio and sidecar files
    for entry in tagged + sidecars:
        dest_file = Path(entry.destination)
        if not dest_file.exists():
            log.error("prune_dest_file_missing", path=str(dest_file))
            return

    # Step 5 — summary, confirmation, delete
    dest_dirs = sorted({Path(e.destination).parent for e in tagged})
    _console.print(f"\n[bold]Source:[/] [cyan]{_markup_escape(str(src_dir))}[/]")
    _console.print("[bold]Annotated tracks written to:[/]")
    for d in dest_dirs:
        _console.print(f"  [green]{_markup_escape(str(d))}[/]")

    if yes:
        shutil.rmtree(src_dir)
        log.info("prune_deleted", path=str(src_dir))
    else:
        prompt_delete_src(src_dir, ui=ui)


def discover(
    src_dirs: list[Path],
    dest_root: Path,
    user_agent: str,
    dry_run: bool = False,
    fetch_rels: bool = True,
    limit: int = 10,
    collision_policy: CollisionPolicy = CollisionPolicy.ASK,
    delete: bool = False,
    ui: DiscoverUI | None = None,
    no_cache: bool = False,
    acoustid_key: str = "",
) -> None:
    """Search MusicBrainz for releases matching each source directory, prompt for confirmation, then apply tags.

    For each directory in ``src_dirs`` the function:

    1. Searches MB using :func:`search_releases_by_dir` and presents a numbered candidate list.
    2. Prompts the user to enter a candidate number, a raw MBID, or ``s`` to skip.
    3. If a valid selection is made, calls :func:`run` to copy and tag that directory.
    4. After a successful copy (unless ``dry_run`` is set and ``delete`` is ``True``), prompts the
       user to delete the original source directory.

    :param src_dirs: List of source directories to process in order.
    :param dest_root: Root destination directory for the annotated music library.
    :param user_agent: MusicBrainz user-agent string.
    :param dry_run: When ``True``, pass through to :func:`run` without writing files; the delete prompt is suppressed.
    :param fetch_rels: When ``False``, skip per-recording relation lookups in :func:`run`.
    :param limit: Maximum number of search candidates to display per directory.
    :param collision_policy: Policy for handling destination file collisions; forwarded to :func:`run`.
    :param delete: When ``True`` and not ``dry_run``, prompt the user to delete each successfully copied source directory.
    :param ui: A :class:`DiscoverUI` instance for user interaction.  Defaults to :class:`TerminalDiscoverUI`.
    :param no_cache: When ``True``, bypass all on-disk metadata and image caches; forwarded to :func:`run`.
    :param acoustid_key: AcoustID application API key.  When set, fingerprints source files and boosts candidates
        whose recordings are confirmed by AcoustID.  Forwarded to :func:`run`.
    """
    if ui is None:
        ui = TerminalDiscoverUI()
    init_mb(user_agent)

    # Read the journal once before the loop so all iterations start with the same baseline
    # snapshot.  The snapshot is refreshed after each successful run() call so that subsequent
    # directories in the same session immediately benefit from MBIDs just written.
    journal = read_journal(dest_root / JOURNAL_FILENAME)

    for src_dir in src_dirs:
        log.info("discover_dir", path=str(src_dir))
        rule = f"[bold cyan]{'=' * 72}[/]"
        _console.print(f"\n{rule}")
        _console.print(f"[bold cyan]Directory:[/] [bold]{_markup_escape(str(src_dir))}[/]")
        _console.print(rule)

        try:
            candidates = search_releases_by_dir(src_dir, limit=limit)
        except ValueError as exc:
            log.warning("discover_skip", reason=str(exc))
            _console.print(f"  [bold yellow]Skipped:[/] [yellow]{exc}[/]")
            continue

        # Enrich organic MB results with journal provenance: any MBID that has already been applied
        # to a tagged file in dest_root is flagged and boosted to score 101 so the user sees it
        # first.  This is the primary mechanism for ensuring multi-disc releases reuse the same
        # MBID across sessions and across discs processed in a single search run.
        journal_ids = _build_journal_release_ids(journal)
        candidates = _enrich_candidates_from_journal(candidates, journal_ids)

        # Apply medium-sequence corroboration: if the source files carry embedded recording-ID tags
        # from a prior tagging run, compare them against each candidate medium's track sequence.
        # In the typical first-run case medium_track_ids_by_release is empty (no full release data
        # has been fetched yet) and this call is a no-op.  The hook is here so that future work
        # (e.g. a pre-fetch step or a multi-disc substrate) can populate the dict and benefit from
        # sequence corroboration without restructuring the discovery flow.
        source_files = find_source_files(src_dir)
        candidates = _enrich_candidates_with_sequence_corroboration(source_files, candidates, {})
        try:
            candidates = _enrich_candidates_with_acoustid_seed(source_files, candidates, acoustid_key)
        except (ValueError, mb.WebServiceError, RuntimeError, OSError) as exc:
            log.error("discover_acoustid_seed_error", path=str(src_dir), error=str(exc), exc_info=True)
            continue

        if not candidates:
            _console.print("  [bold yellow]No candidates found.[/]")
            continue

        release_id = ui.choose_release(src_dir, candidates)
        if release_id is None:
            log.info("discover_skipped", path=str(src_dir))
            continue

        log.info("discover_selected", release_id=release_id, src_dir=str(src_dir))

        # Whipper recognition (C-WHIP): set origin_source and parse AccurateRip data when
        # either strong signature is present.  The AR data is passed to run() so it can be
        # threaded into TransactionEntry flat fields and ProvenanceSidecar.accuraterip_summary.
        # Download recognition (C-DL) runs only when whipper is not recognised — mutual exclusion.
        whipper = is_whipper_dir(src_dir)
        if whipper:
            origin_source = "whipper"
            log.info("whipper_dir_recognised", src_dir=str(src_dir))
        elif is_download_dir(src_dir):
            origin_source = "download"
            log.info("download_dir_recognised", src_dir=str(src_dir))
        else:
            origin_source = ""
        ar_summary, ar_tracks = _parse_whipper_ar(src_dir) if whipper else (AccurateRipSummary(), {})

        try:
            run(
                release_id=release_id,
                src_dir=src_dir,
                dest_root=dest_root,
                user_agent=user_agent,
                dry_run=dry_run,
                fetch_rels=fetch_rels,
                collision_policy=collision_policy,
                ui=ui,
                no_cache=no_cache,
                acoustid_key=acoustid_key,
                origin_source=origin_source,
                ar_summary=ar_summary,
                ar_tracks=ar_tracks,
            )
        except (ValueError, mb.WebServiceError, RuntimeError, OSError) as exc:
            log.error("discover_run_error", release_id=release_id, error=str(exc), exc_info=True)
            continue

        # Refresh the journal snapshot so the next directory in this session sees the MBID just
        # written by run().  This is what makes sibling-disc consistency work within a single
        # multi-directory search invocation.
        journal = read_journal(dest_root / JOURNAL_FILENAME)

        if not dry_run and delete:
            prompt_delete_src(src_dir, ui=ui)
