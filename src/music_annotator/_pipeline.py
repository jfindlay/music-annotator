"""Top-level annotation pipeline for music-annotator.

Provides :func:`run`, the main entry point that copies and tags a classical music album using
MusicBrainz metadata.  Also provides :class:`CollisionPolicy`, :func:`_select_medium`,
:func:`_prompt_collision_policy`, and :func:`_prompt_duration_warnings` as extracted helpers.
"""

from __future__ import annotations

import datetime
import enum
import errno
import hashlib
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Protocol

import structlog
from mutagen._util import MutagenError
from rich.markup import escape as _markup_escape

from music_annotator._artists import artist_credit_phrase
from music_annotator._console import _console
from music_annotator._mb_api import (
    _fetch_acoustid_lookup_raw,
    _get_bottom_work,
    fetch_acoustid_id,
    fetch_acoustid_lookup,
    fetch_cover_art,
    fetch_recording_detail,
    fetch_release,
    init_mb,
)
from music_annotator._pipeline_io import (
    _DISC_INFO_FILENAME,
    JOURNAL_FILENAME,
    AudioCompareResult,
    _assess_collisions,
    _audio_hash,
    _confirm_fragmentation,
    _needs_enrich,
    _read_duration_ms,
    _read_tags_flac,
    _read_tags_mp3,
    _run_fpcalc,
    _sha256_file,
    _verify_copy,
    check_duration_preflight,
    detect_fragmented_releases,
    find_source_files,
    parse_disc_title,
    parse_disc_toc,
    read_journal,
    write_transaction_log,
)
from music_annotator._tagger import apply_tags_flac, apply_tags_mp3
from music_annotator._tags import _NAME_MAX, _proposed_short, build_dest_path, build_track_tags
from music_annotator._works import build_work_hierarchy, select_primary_performance_work
from music_annotator.models import (
    CopyPlanEntry,
    CoverArt,
    CoverImage,
    MBMedium,
    MBRelease,
    MBTrack,
    MBWork,
    TrackTags,
    TransactionEntry,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Disc-number suffix such as "(Disc 1)", "(disc 2)", or "Disc 1" at the end of a dir name.
_DISC_SUFFIX_RE: re.Pattern[str] = re.compile(r"\s*[\(\[]?[Dd]isc\s*\d+[\)\]]?\s*$")


class CollisionPolicy(enum.Enum):
    """Policy to apply when destination files already exist.

    Consulted by :func:`run` after :func:`_check_collisions` reports that one or more destination
    files already exist on disk before copying begins.

    - ``ASK`` (default): prompt the user interactively via :func:`_prompt_collision_policy`.
    - ``OVERWRITE``: silently replace all existing files.
    - ``SKIP``: Copy only files that do not already exist; leave existing files untouched.
    - ``ABORT``: raise :exc:`SystemExit` with code 1 without copying anything.
    """

    ASK = "ask"
    OVERWRITE = "overwrite"
    SKIP = "skip"
    ABORT = "abort"


class DiscUI(Protocol):
    """Minimal UI protocol for disc-selection and name-shortening confirmation in :func:`run`.

    A structural subset of :class:`~music_annotator._discover.DiscoverUI` — any object that
    implements :meth:`confirm_disc` and :meth:`confirm_shortened_name` satisfies this protocol.
    Defined here (rather than importing from ``_discover``) to avoid a circular import:
    ``_discover`` imports :func:`run` from this module.
    """

    def confirm_disc(
        self,
        mediums: list[MBMedium],
        proposed: MBMedium,
        dtitle: str,
        release_url: str,
    ) -> MBMedium | None:
        """Confirm or override the proposed disc selection.

        :param mediums: All mediums for the release.
        :param proposed: The medium selected by the heuristic.
        :param dtitle: FreeDB disc title used for the match.
        :param release_url: MusicBrainz release URL.
        :returns: The confirmed or user-chosen :class:`~music_annotator.models.MBMedium`, or ``None``
            to abort.
        """  # pragma: no cover

    def confirm_shortened_name(self, original: str, proposed: str) -> str | None:
        """Confirm or override a path component that exceeds :data:`~music_annotator._tags._NAME_MAX` bytes.

        Called once per unique too-long component before any files are written.  The user may
        accept the proposed shortened name, type a custom replacement, or abort the run.

        :param original: The full computed path component that exceeds the byte limit (displayed for context).
        :param proposed: The auto-shortened component produced by
            :func:`~music_annotator._tags._proposed_short` (displayed as the default choice).
        :returns: The confirmed replacement string (either ``proposed`` or a user-supplied override),
            or ``None`` to abort.
        """  # pragma: no cover


def _match_medium_by_toc(mediums: list[MBMedium], track_frames: list[int]) -> MBMedium | None:
    """Return the medium whose disc TOC matches ``track_frames`` within a ±1 frame tolerance, or ``None``.

    Compares ``track_frames`` (per-track CD frame offsets from the source directory's
    ``00 - disc info.yaml``) against the ``offsets`` list on every :class:`~music_annotator.models.MBDisc`
    entry attached to each medium.  A medium may carry more than one disc entry (different pressings),
    so all are checked.

    A tolerance of ±1 CD frame (~13 ms) per offset is applied because ripping software (e.g. dBpowerAMP,
    EAC, Whipper) and MusicBrainz can differ by a single frame in how they count the track start position,
    due to rounding or read-offset conventions.  This tolerance is tight enough to be unambiguous in
    practice — adjacent movements on a disc are thousands of frames apart.

    An exact match is logged at ``info`` level; a fuzzy match (any offset differs by exactly 1) is logged
    at ``warning`` level so the operator can verify the selection manually if needed.

    Returns ``None`` when no medium has a matching disc, or when the release was fetched without
    ``discids`` includes (i.e. all ``disc_list`` fields are empty).

    :param mediums: The list of :class:`~music_annotator.models.MBMedium` objects from the release.
    :param track_frames: Per-track CD frame start offsets extracted from ``00 - disc info.yaml``.
    :returns: The matching :class:`~music_annotator.models.MBMedium`, or ``None`` if no match is found.
    """
    for medium in mediums:
        for disc in medium.disc_list:
            if len(disc.offsets) != len(track_frames):
                continue
            if disc.offsets == track_frames:
                log.info("toc_match_exact", position=medium.position, offsets=track_frames)
                return medium
            if all(abs(a - b) <= 1 for a, b in zip(disc.offsets, track_frames)):
                log.warning(
                    "toc_match_fuzzy",
                    position=medium.position,
                    mb_offsets=disc.offsets,
                    yaml_offsets=track_frames,
                )
                return medium
    return None


class SelectionMethod(enum.Enum):
    """How :func:`_select_medium_with_reason` chose the medium within a multi-medium release.

    Used by :func:`run` to decide whether an interactive disc-confirmation prompt is required.

    - ``TOC`` — exact or ±1 frame TOC offset match against MB disc IDs; highly reliable.
    - ``TRACK_COUNT`` — unique track-count match; reliable when only one medium has ``n_src`` tracks.
    - ``TITLE`` — FreeDB DTITLE token overlap with MB medium/track titles; heuristic, requires
      user confirmation.
    - ``FALLBACK`` — track-count tie with no better signal; disc-number hint or first-match; requires
      user confirmation when the title match was also ambiguous.
    """

    TOC = "toc"
    TRACK_COUNT = "track_count"
    TITLE = "title"
    FALLBACK = "fallback"


def _score_medium_title(medium: MBMedium, dtitle_tokens: set[str]) -> int:
    """Return the number of ``dtitle_tokens`` that appear in ``medium``'s combined title text.

    The combined text is the medium's own subtitle (if any) concatenated with the title of its
    first track's recording.  Both are lower-cased and split on non-alphanumeric characters before
    comparison, so that punctuation differences between FreeDB and MB don't prevent matches.

    :param medium: The :class:`~music_annotator.models.MBMedium` to score.
    :param dtitle_tokens: Lower-cased alphanumeric tokens from the FreeDB DTITLE title portion.
    :returns: Count of tokens shared between ``dtitle_tokens`` and the medium's text tokens.
    """

    def _tokenise(text: str) -> set[str]:
        return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w}

    combined = medium.title
    if medium.track_list:
        combined = f"{combined} {medium.track_list[0].recording.title}"
    return len(dtitle_tokens & _tokenise(combined))


def _match_medium_by_title(mediums: list[MBMedium], n_src: int, dtitle: str) -> MBMedium | None:
    """Return the medium that best matches the FreeDB disc title ``dtitle``, or ``None``.

    Restricts candidates to mediums whose track count equals ``n_src``, then scores each by counting
    shared alphanumeric tokens between ``dtitle`` and the medium's combined title text (medium subtitle
    + first track title).  Returns the best-scoring medium only when it scores strictly higher than
    all others and has a non-zero score; ties and zero scores return ``None``.

    Always logs a ``warning`` so the operator knows a heuristic match was used.

    :param mediums: All mediums for the release.
    :param n_src: Number of source audio files; only mediums with this track count are considered.
    :param dtitle: FreeDB disc title (title portion of ``DTITLE``, after the `` / `` separator).
    :returns: The best-matching :class:`~music_annotator.models.MBMedium`, or ``None`` if the match
        is ambiguous or no medium scores above zero.
    """
    candidates = [m for m in mediums if len(m.track_list) == n_src]
    if not candidates or not dtitle:
        return None

    dtitle_tokens: set[str] = {w for w in re.split(r"[^a-z0-9]+", dtitle.lower()) if w}
    if not dtitle_tokens:
        return None

    scores = [(m, _score_medium_title(m, dtitle_tokens)) for m in candidates]
    scores.sort(key=lambda x: x[1], reverse=True)
    best_medium, best_score = scores[0]
    runner_up_score = scores[1][1] if len(scores) > 1 else 0

    log.warning(
        "title_match_heuristic",
        dtitle=dtitle,
        best_position=best_medium.position,
        best_score=best_score,
        runner_up_score=runner_up_score,
    )

    if best_score > 0 and best_score > runner_up_score:
        return best_medium
    return None


def _select_medium_with_reason(
    mediums: list[MBMedium],
    n_src: int,
    src_dir_name: str,
    track_frames: list[int] | None = None,
    dtitle: str = "",
) -> tuple[MBMedium, SelectionMethod]:
    """Select the correct medium from a multi-medium release and return why it was chosen.

    Selection strategy (in order):

    1. **TOC match**: if ``track_frames`` is provided, compare against each medium's disc TOC entries
       via :func:`_match_medium_by_toc`.  Matches within ±1 frame per offset; fuzzy match logged.
    2. **Track-count unique**: if exactly one medium has ``len(track_list) == n_src``, return it.
    3. **Title match**: if ``dtitle`` is provided and multiple mediums share the track count, score
       each medium by FreeDB token overlap via :func:`_match_medium_by_title`.  Returns
       :attr:`SelectionMethod.TITLE` so the caller can prompt the user for confirmation.
    4. **Disc-number hint**: check ``src_dir_name`` for a disc-number suffix (e.g. ``"(Disc 2)"``).
    5. **Fallback**: return the first track-count-matching medium.

    :param mediums: The list of :class:`~music_annotator.models.MBMedium` objects from the release.
    :param n_src: Number of source audio files in the directory.
    :param src_dir_name: The directory basename used to extract a disc-number hint.
    :param track_frames: Optional per-track CD frame offsets from ``00 - disc info.yaml``.
    :param dtitle: Optional FreeDB disc title from ``00 - disc info.yaml`` for title-based matching.
    :returns: A ``(medium, method)`` pair.
    :raises ValueError: When no medium matches the source file count.
    """
    # --- Priority 1: TOC match ---
    if track_frames:
        toc_match = _match_medium_by_toc(mediums, track_frames)
        if toc_match is not None:
            log.info("multi_disc_medium_selected_by_toc", position=toc_match.position, tracks=n_src)
            return toc_match, SelectionMethod.TOC

    # --- Priority 2: track-count unique ---
    matching = [m for m in mediums if len(m.track_list) == n_src]
    if len(matching) == 1:
        selected = matching[0]
        log.info("multi_disc_medium_selected", position=selected.position, tracks=n_src)
        return selected, SelectionMethod.TRACK_COUNT

    if len(matching) > 1:
        # --- Priority 3: title match ---
        if dtitle:
            title_match = _match_medium_by_title(mediums, n_src, dtitle)
            if title_match is not None:
                return title_match, SelectionMethod.TITLE

        # --- Priority 4: disc-number hint ---
        disc_hint_match = _DISC_SUFFIX_RE.search(src_dir_name)
        if disc_hint_match:
            hint_digits = re.search(r"\d+", disc_hint_match.group())
            hint_pos = int(hint_digits.group()) if hint_digits else 0
            hinted = [m for m in matching if m.position == hint_pos]
            selected = hinted[0] if hinted else matching[0]
        else:
            selected = matching[0]
        log.info("multi_disc_medium_selected", position=selected.position, tracks=n_src)
        return selected, SelectionMethod.FALLBACK

    # No exact track-count match — list available mediums and abort.
    medium_info = [(m.position, len(m.track_list)) for m in mediums]
    raise ValueError(
        f"track count mismatch: source directory has {n_src} file(s) but no medium matches. "
        f"Available mediums (position, tracks): {medium_info}. "
        "Re-run with the correct --release-id for this disc, or ensure the source directory "
        "contains exactly the tracks for one medium."
    )


def _prompt_collision_policy(results: list[AudioCompareResult], dest_root: Path) -> CollisionPolicy:
    """Print a collision warning (with audio-comparison context) and prompt the user for a resolution policy.

    Groups the conflicting files by their work directory (``parts[0] / parts[1]`` relative to
    ``dest_root``) so the user sees the ``[rec YYYY]`` / ``[rel YYYY]`` date suffix on each
    destination without the absolute prefix overwhelming the terminal width.  Individual
    conflicting files are listed as relative paths (including any intermediate Act subdirectory)
    beneath the directory summary, each annotated with its audio-comparison result.  All path
    strings are escaped before passing to Rich so that ``[rec YYYY]`` / ``[rel YYYY]`` brackets
    are not interpreted as markup tags.  Re-prompts until a valid choice is entered.

    Only receives results whose ``match`` is ``True`` or ``None`` (confirmed identical or
    inconclusive); non-matching collisions are handled by :func:`_apply_collision_suffix` before
    this function is called.

    :param results: :class:`~music_annotator._pipeline_io.AudioCompareResult` objects for the
        confirmed-identical and inconclusive collisions.
    :param dest_root: Root directory of the destination library, used to derive the work dir
        for display grouping.
    :returns: The :class:`CollisionPolicy` chosen by the user.
    """
    collisions = [r.dest for r in results]
    work_dirs = sorted({p.relative_to(dest_root).parts[0] / Path(p.relative_to(dest_root).parts[1]) for p in collisions})
    _console.print(
        f"\n[bold red]WARNING:[/] [red]{len(collisions)} destination file(s) already exist under "
        f"{_markup_escape(str(dest_root))}:[/]"
    )
    for d in work_dirs:
        _console.print(f"  [red]{_markup_escape(str(d))}[/]")
    _console.print("\n[red]Conflicting files (with audio comparison result):[/]")
    result_by_dest = {r.dest: r for r in results}
    for p in sorted(collisions, key=lambda x: x.name):
        r = result_by_dest[p]
        match_label = "[yellow]inconclusive[/]" if r.match is None else "[cyan]identical[/]"
        _console.print(
            f"  [red]{_markup_escape(str(p.relative_to(dest_root)))}[/]  ({match_label}: {_markup_escape(r.detail)})"
        )
    _console.print("\n[bold]Choose an action:[/]")
    _console.print("  [bold red]\\[a] abort[/]     — quit without copying anything")
    _console.print("  [bold yellow]\\[s] skip[/]      — copy only new files, leave existing untouched")
    _console.print("  [bold green]\\[o] overwrite[/] — replace all existing files")
    while True:
        _console.print("\n[bold cyan]>[/] ", end="")
        choice = input("").strip().lower()
        match choice:
            case "o" | "overwrite":
                log.info("collision_choice_overwrite", count=len(collisions))
                return CollisionPolicy.OVERWRITE
            case "s" | "skip":
                log.info("collision_choice_skip", count=len(collisions))
                return CollisionPolicy.SKIP
            case "a" | "abort":
                log.warning("collision_choice_abort")
                return CollisionPolicy.ABORT
            case _:
                _console.print("[yellow]Please enter 'a', 's', or 'o'.[/]")


def _prompt_duration_warnings(warnings: list[str]) -> bool:
    """Print per-track duration mismatch warnings and prompt the user to confirm or abort.

    Called when :func:`check_duration_preflight` returns a non-empty list.  Displays each
    warning line and asks the user whether to proceed.  Re-prompts until a valid choice
    is entered.

    Only called when ``dry_run`` is ``False`` and warnings are present; skipped entirely
    in dry-run mode to preserve the non-interactive contract for automation.

    :param warnings: List of human-readable warning strings from :func:`check_duration_preflight`,
        one per mismatched track.
    :returns: ``True`` if the user chooses to proceed despite the warnings, ``False`` if the user
        chooses to abort.
    """
    _console.print(
        f"\n[bold yellow]WARNING:[/] [yellow]{len(warnings)} source file(s) have audio durations "
        "that differ significantly from the MusicBrainz release data:[/]"
    )
    for w in warnings:
        _console.print(f"[yellow]{w}[/]")
    _console.print(
        "\n[yellow]This may indicate the wrong release MBID was supplied, or that MB duration data "
        "is inaccurate for this pressing.[/]"
    )
    _console.print("\n[bold]Choose an action:[/]")
    _console.print("  [bold green]\\[p] proceed[/] — continue despite the duration mismatch(es)")
    _console.print("  [bold red]\\[a] abort[/]   — quit without copying anything")
    while True:
        _console.print("\n[bold cyan]>[/] ", end="")
        choice = input("").strip().lower()
        match choice:
            case "p" | "proceed":
                log.info("duration_preflight_proceed", mismatched=len(warnings))
                return True
            case "a" | "abort":
                log.warning("duration_preflight_abort", mismatched=len(warnings))
                return False
            case _:
                _console.print("[yellow]Please enter 'p' or 'a'.[/]")


def _collision_suffix(release: MBRelease) -> str:
    """Return a short release-identifying suffix string for path disambiguation.

    Used by :func:`_apply_collision_suffix` to form the bracketed suffix appended to the
    ``work_dir`` component of a destination path when the incoming audio is confirmed to be
    different content from the existing file at that path.

    Preference order:

    1. The catalog number of the first :class:`~music_annotator.models.MBLabelInfo` entry, if
       non-empty.
    2. The first 8 characters of the release MBID — guaranteed unique and always present.

    :param release: The :class:`~music_annotator.models.MBRelease` being processed.
    :returns: A non-empty suffix string suitable for appending as ``[<suffix>]``.
    """
    if release.label_info_list:
        cat = release.label_info_list[0].catalog_number.strip()
        if cat:
            return cat
    return release.id[:8]


def _apply_collision_suffix(
    plan: list[CopyPlanEntry],
    nonmatches: list[AudioCompareResult],
    release: MBRelease,
    dest_root: Path,
) -> None:
    """Rewrite destination paths in ``plan`` for confirmed non-matching collisions.

    For each :class:`~music_annotator._pipeline_io.AudioCompareResult` in ``nonmatches``, finds
    the corresponding :class:`~music_annotator.models.CopyPlanEntry` (matched by ``dest_file``)
    and appends a release-identifying suffix to the ``work_dir`` component (``parts[1]`` relative
    to ``dest_root``) of its destination path.

    The suffix is derived by :func:`_collision_suffix` — catalog number when available, 8-char
    MBID prefix otherwise — and is formatted as ``[<suffix>]`` appended to the existing work-dir
    name.  This keeps the new destination unique while preserving the work-dir name and date
    label so the library stays navigable.

    Mutates ``plan`` in-place.  Logs a ``collision_nonmatch_suffix`` warning for each affected
    entry so the operator can verify the rename in the journal.

    :param plan: The list of :class:`~music_annotator.models.CopyPlanEntry` objects; mutated
        in-place.
    :param nonmatches: Confirmed non-matching :class:`~music_annotator._pipeline_io.AudioCompareResult`
        objects (``match=False``) for which the destination path must be disambiguated.
    :param release: The :class:`~music_annotator.models.MBRelease` being processed; used to
        derive the suffix via :func:`_collision_suffix`.
    :param dest_root: Root of the destination library; used to isolate the ``work_dir`` component
        at relative depth 1 that receives the suffix.
    """
    suffix = _collision_suffix(release)
    nonmatch_dests = {r.dest for r in nonmatches}

    for i, entry in enumerate(plan):
        if entry.dest_file not in nonmatch_dests:
            continue
        # Rewrite parts[1] (work_dir, at relative depth 1) to add the release suffix.
        # Destination structure: dest_root / parts[0] / parts[1] / [intermediate/] leaf
        rel_parts = list(entry.dest_file.relative_to(dest_root).parts)
        # rel_parts[0] = composer_dir, rel_parts[1] = work_dir, rel_parts[2:] = rest
        rel_parts[1] = f"{rel_parts[1]} [{suffix}]"
        new_dest = dest_root.joinpath(*rel_parts)
        log.warning(
            "collision_nonmatch_suffix",
            original=str(entry.dest_file.relative_to(dest_root)),
            renamed=str(new_dest.relative_to(dest_root)),
            method=next(r.method for r in nonmatches if r.dest == entry.dest_file),
            suffix=suffix,
        )
        plan[i] = CopyPlanEntry(idx=entry.idx, src_file=entry.src_file, dest_file=new_dest)


def _write_sidecars(
    cover: CoverArt,
    work_top_dir: Path,
    sidecars_written: set[Path],
    journal_entries: list[TransactionEntry],
    now: str,
    release_id: str,
) -> None:
    """Write sidecar cover art files for ``work_top_dir`` and append journal entries.

    Called after every successful :func:`_verify_copy` for a track.  Two deduplication guards
    ensure correct behaviour:

    1. **Directory-level**: ``sidecars_written`` (keyed on ``work_top_dir``) prevents the
       function from running more than once for the same work directory within a run — i.e. when
       the directory contains multiple tracks.

    2. **Path-level**: ``seen_sidecar_paths`` (local, keyed on the resolved destination path)
       prevents a CAA image that carries multiple type tags (e.g. ``["Back", "Spine"]``) from
       being written and journalled twice.  The Cover Art Archive API allows a single image to
       belong to more than one type, and :func:`~music_annotator._mb_api.fetch_cover_art` reuses
       the same :class:`~music_annotator.models.CoverImage` object across the corresponding
       :class:`~music_annotator.models.CoverArt` bucket lists to avoid duplicate downloads.
       Without the path-level guard the concatenated ``sidecar_images`` list would contain the
       same object twice — once per bucket — producing a duplicate journal entry and a redundant
       overwrite of the file on disk.

    Writes every :class:`~music_annotator.models.CoverImage` from all non-front sidecar lists
    on ``cover`` (``front_full``, ``back``, ``booklet``, ``medium``, ``tray``, ``obi``,
    ``spine``, ``track``, ``liner``, ``sticker``, ``poster``, ``matrix``, ``top``, ``bottom``,
    ``panel``, ``watermark``, ``raw``, ``other``, ``unknown``) that has a non-empty ``filename``
    field.  For each written sidecar an ``action="downloaded"``
    :class:`~music_annotator.models.TransactionEntry` is appended to ``journal_entries`` with
    ``source`` set to the canonical CAA URL (so the file can be re-downloaded from the journal
    alone).

    :param cover: The :class:`~music_annotator.models.CoverArt` instance for this release.
    :param work_top_dir: The work top directory (``<dest_root>/<composer-dir>/<work-dir>``).
    :param sidecars_written: Mutable set of directories that have already received sidecar files.
    :param journal_entries: Mutable list to which new entries are appended.
    :param now: ISO-format timestamp string for journal entries.
    :param release_id: MusicBrainz release MBID for journal entries.
    """
    if work_top_dir in sidecars_written:
        return
    sidecars_written.add(work_top_dir)

    sidecar_images: list[CoverImage] = (
        list(cover.front_full)
        + list(cover.back)
        + list(cover.booklet)
        + list(cover.medium)
        + list(cover.tray)
        + list(cover.obi)
        + list(cover.spine)
        + list(cover.track)
        + list(cover.liner)
        + list(cover.sticker)
        + list(cover.poster)
        + list(cover.matrix)
        + list(cover.top)
        + list(cover.bottom)
        + list(cover.panel)
        + list(cover.watermark)
        + list(cover.raw)
        + list(cover.other)
        + list(cover.unknown)
    )
    seen_sidecar_paths: set[Path] = set()
    for img in sidecar_images:
        if not img.filename:
            continue
        sidecar_path = work_top_dir / img.filename
        if sidecar_path in seen_sidecar_paths:
            continue
        seen_sidecar_paths.add(sidecar_path)
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        expected_hash = hashlib.sha256(img.data).hexdigest()
        sidecar_path.write_bytes(img.data)
        written_hash = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
        if written_hash != expected_hash:
            raise RuntimeError(
                f"sidecar write integrity failure for '{sidecar_path.name}': "
                f"expected SHA-256 {expected_hash[:12]}… ≠ written SHA-256 {written_hash[:12]}…"
            )
        log.debug("sidecar_written", path=str(sidecar_path))
        journal_entries.append(
            TransactionEntry(
                timestamp=now,
                release_id=release_id,
                source=img.url,
                destination=str(sidecar_path),
                action="downloaded",
            )
        )


def _write_freedb_yaml(
    src_dir: Path,
    work_top_dir: Path,
    medium_pos: int,
    freedb_written: set[Path],
    journal_entries: list[TransactionEntry],
    now: str,
    release_id: str,
) -> None:
    """Copy ``00 - disc info.yaml`` from ``src_dir`` to ``work_top_dir/freedb_disc_N.yaml``.

    Preserves FreeDB disc data losslessly in the destination library so it can later be used for
    submitting disc IDs to MusicBrainz, Discogs lookups, or further analysis.

    The destination filename is ``freedb_disc_{medium_pos}.yaml`` so that multi-disc releases where
    both discs share a ``work_top_dir`` each receive their own numbered file without overwriting.

    A SHA-256 hash of the source file is computed before the copy and verified against the
    destination after the copy; a mismatch raises :exc:`RuntimeError`.  A ``"sidecar"`` journal entry
    is appended on success.

    A set ``freedb_written`` keyed on the destination path ensures the copy is performed at most once
    per ``work_top_dir + medium_pos`` combination across all tracks in the copy loop.

    :param src_dir: Source directory that may contain a ``00 - disc info.yaml`` file.
    :param work_top_dir: Work top directory (``dest_root / composer-dir / work-dir``) where the YAML
        is written.
    :param medium_pos: 1-based disc position, used to form the destination filename.
    :param freedb_written: Mutable set of destination paths already written this run.
    :param journal_entries: Mutable list to which a new ``"sidecar"`` entry is appended.
    :param now: ISO-format timestamp string for the journal entry.
    :param release_id: MusicBrainz release MBID for the journal entry.
    :raises RuntimeError: When the SHA-256 of the written file does not match the source.
    """
    yaml_src = src_dir / _DISC_INFO_FILENAME
    if not yaml_src.is_file():
        return
    dest_yaml = work_top_dir / f"freedb_disc_{medium_pos}.yaml"
    if dest_yaml in freedb_written:
        return
    freedb_written.add(dest_yaml)
    src_data = yaml_src.read_bytes()
    src_hash = hashlib.sha256(src_data).hexdigest()
    shutil.copy2(yaml_src, dest_yaml)
    dest_hash = hashlib.sha256(dest_yaml.read_bytes()).hexdigest()
    if dest_hash != src_hash:
        raise RuntimeError(
            f"freedb yaml copy integrity failure for '{dest_yaml.name}': "
            f"src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {dest_hash[:12]}…"
        )
    log.debug("freedb_yaml_written", dest=str(dest_yaml))
    journal_entries.append(
        TransactionEntry(
            timestamp=now,
            release_id=release_id,
            source=str(yaml_src),
            destination=str(dest_yaml),
            action="sidecar",
        )
    )


def _warn_long_names(plan: list[CopyPlanEntry], dest_root: Path) -> None:
    """Log a warning for every path component in ``plan`` that exceeds :data:`~music_annotator._tags._NAME_MAX` bytes.

    Called in dry-run mode so the operator can identify problems without touching the filesystem.
    No prompting or shortening is performed.

    :param plan: The list of planned copy operations.
    :param dest_root: Root of the destination library, used to isolate relative components.
    """
    seen: set[str] = set()
    for entry in plan:
        for part in entry.dest_file.relative_to(dest_root).parts:
            if len(part.encode("utf-8")) > _NAME_MAX and part not in seen:
                seen.add(part)
                log.warning(
                    "name_too_long",
                    component=part,
                    bytes=len(part.encode("utf-8")),
                    limit=_NAME_MAX,
                )


def _resolve_long_names(plan: list[CopyPlanEntry], dest_root: Path, ui: DiscUI | None) -> list[CopyPlanEntry]:
    """Detect path components that exceed :data:`~music_annotator._tags._NAME_MAX` bytes and resolve them.

    Iterates every component of every destination path in ``plan``.  For each unique component
    that is too long, a shortened replacement is determined:

    - If ``ui`` is provided: calls :meth:`DiscUI.confirm_shortened_name` with the original and
      the auto-proposed shortened name.  If the user aborts (returns ``None``), raises
      :exc:`SystemExit` with code 1.
    - If ``ui`` is ``None``: silently accepts :func:`~music_annotator._tags._proposed_short` and
      logs a ``name_too_long`` warning.

    The substitution table is built first (prompting once per unique too-long component), then
    applied to every entry in the plan so that shared components (e.g. the top-level composer
    directory) are renamed consistently across all tracks.

    :param plan: The list of planned copy operations.
    :param dest_root: Root of the destination library.
    :param ui: Optional :class:`DiscUI` instance.  When ``None``, shortened names are accepted
        automatically.
    :returns: A new plan list with updated destination paths.
    :raises SystemExit: With code 1 if the user aborts any name-shortening prompt.
    """
    # Build substitution table: original component → replacement.
    subs: dict[str, str] = {}
    for entry in plan:
        rel_parts = entry.dest_file.relative_to(dest_root).parts
        for part in rel_parts:
            # For the leaf, measure the stem only (the extension is not part of NAME_MAX for the stem;
            # actually the whole filename including extension counts, but the extension is short and
            # we store stems before adding the suffix — measure the full part).
            if len(part.encode("utf-8")) > _NAME_MAX and part not in subs:
                proposed = _proposed_short(part)
                if ui is not None:
                    replacement = ui.confirm_shortened_name(part, proposed)
                    if replacement is None:
                        log.warning("name_shortening_aborted", component=part)
                        raise SystemExit(1)
                else:
                    replacement = proposed
                    log.warning(
                        "name_too_long",
                        component=part,
                        bytes=len(part.encode("utf-8")),
                        limit=_NAME_MAX,
                        shortened=replacement,
                    )
                subs[part] = replacement

    if not subs:
        return plan

    # Apply substitutions to all plan entries.
    new_plan: list[CopyPlanEntry] = []
    for entry in plan:
        orig_parts = entry.dest_file.relative_to(dest_root).parts
        new_parts = [subs.get(p, p) for p in orig_parts]
        new_dest = dest_root.joinpath(*new_parts)
        new_plan.append(CopyPlanEntry(idx=entry.idx, src_file=entry.src_file, dest_file=new_dest))
    return new_plan


def run(
    release_id: str,
    src_dir: Path,
    dest_root: Path,
    user_agent: str,
    dry_run: bool = False,
    fetch_rels: bool = True,
    collision_policy: CollisionPolicy = CollisionPolicy.ASK,
    ui: DiscUI | None = None,
    no_cache: bool = False,
    disc_override: int | None = None,
    acoustid_key: str = "",
) -> None:
    """Copy and tag an album directory using MusicBrainz metadata.

    This is the top-level entry point for the annotation pipeline:

    1. Initialise the MB user-agent and fetch the release.
    2. Fetch cover art from the Cover Art Archive.
    3. For every track on every medium of the release (aggregation pass):

       a. Fetch the recording's artist and work relationships.
       b. Walk up the work hierarchy to build ``cwp_work_N`` levels.
       c. Build the full :class:`~music_annotator.models.TrackTags` model.

    4. Compute movement numbers and totals grouped by top-work MBID across all media.
    5. Compute destination paths and check for collisions for the **selected medium only**
       (copy-subset).  If any destination files already exist (and ``dry_run`` is ``False``),
       the ``collision_policy`` determines behaviour:

       * :attr:`CollisionPolicy.ASK` — prompt interactively (default).
       * :attr:`CollisionPolicy.OVERWRITE` — replace all conflicting files.
       * :attr:`CollisionPolicy.SKIP` — copy only new files, leave existing files untouched.
       * :attr:`CollisionPolicy.ABORT` — raise :exc:`SystemExit` without copying anything.

    6. Copy each source file (selected medium only) to the destination tree, apply tags, and
       restore source timestamps.
    7. Append a :class:`~music_annotator.models.TransactionEntry` per file to
       ``<dest_root>/music_annotator_journal.json`` (created or updated atomically).

    :param release_id: The MusicBrainz release MBID.
    :param src_dir: Directory containing the source audio files.  Files are matched to release tracks by sorted filename
        order.  For multi-medium releases the correct disc is identified first by matching the CD frame offsets from
        ``00 - disc info.yaml`` (if present) against each medium's disc TOC data, then by track count, then by FreeDB
        title token matching, then by a disc-number suffix in the directory name.  When the title-match heuristic is
        used, ``ui.confirm_disc`` is called to prompt the user unless ``dry_run`` is ``True``.  A :exc:`ValueError` is
        raised when no medium can be matched.  When ``disc_override`` is set, all heuristics are bypassed and the
        medium at that 1-based position is selected directly; a :exc:`ValueError` is raised if the position is not
        found in the release.
    :param dest_root: Root directory of the destination music library.
    :param user_agent: User-agent string passed to :func:`init_mb`.
    :param dry_run: When ``True``, log planned operations without copying or writing any files.  MB API calls for the
        release and recording relations still happen so the planned tag data is logged accurately.  The collision prompt,
        disc-selection prompt, and the journal write are also skipped.
    :param fetch_rels: When ``False``, skip per-recording lookups and produce minimal tags (faster but incomplete).
        Composer, conductor, work hierarchy, and Classical Extras tags will be absent.
    :param collision_policy: How to handle pre-existing destination files.  Defaults to
        :attr:`CollisionPolicy.ASK` which prompts interactively.
    :param ui: Optional :class:`DiscUI` instance for interactive disc-selection and name-shortening confirmation.  When
        ``None`` and a title-match heuristic is used, the selection proceeds without confirmation.  When ``None`` and a
        path component exceeds :data:`~music_annotator._tags._NAME_MAX` bytes, the shortened name is accepted
        automatically and a ``name_too_long`` warning is logged.
    :param no_cache: When ``True``, bypass the cover art on-disk cache and always fetch from the network.  Defaults to
        ``False``.
    :param disc_override: When set, bypass all automatic medium-selection heuristics and use the medium at this
        1-based disc position.  Applies to both single-medium and multi-medium releases.  The downstream track-count
        validation still runs, so a mismatch between source file count and the selected medium's track count raises
        :exc:`RuntimeError`.
    :param acoustid_key: AcoustID application API key.  When set, performs a keyed fingerprint lookup for each source
        file after the per-track :func:`fetch_acoustid_id` loop and logs whether the selected recording MBID is
        confirmed or contradicted by the AcoustID results.  Never alters the copy/tag/verify path.
    :raises mb.ResponseError: On a non-retryable MusicBrainz API error.
    :raises RuntimeError: If all retry attempts are exhausted for any API call, or if post-copy verification fails (copy
        integrity, tag round-trip, cover art, or mtime mismatch).
    :raises ValueError: If no medium in the release matches the source file count for a multi-medium release, or if
        ``disc_override`` specifies a position that does not exist in the release.
    :raises OSError: If source files cannot be read or destination files cannot be written.
    :raises SystemExit: With code 1 if the collision policy is ABORT (or the user chooses abort interactively), if the
        user aborts the disc-selection confirmation prompt, if the user aborts a name-shortening prompt, or if the user
        aborts the duration pre-flight confirmation prompt.
    """
    init_mb(user_agent)

    log.info("fetch_release_start", release_id=release_id)
    release = fetch_release(release_id)
    log.info("fetch_release_done", title=release.title, date=release.date)

    src_files = find_source_files(src_dir)
    log.info("source_files", count=len(src_files))

    toc = parse_disc_toc(src_dir)
    track_frames = toc[2] if toc is not None else None
    dtitle = parse_disc_title(src_dir)

    mediums = release.medium_list

    if disc_override is not None:
        hits = [m for m in mediums if m.position == disc_override]
        if not hits:
            medium_info = [(m.position, len(m.track_list)) for m in mediums]
            raise ValueError(
                f"--disc {disc_override} not found in release '{release.title}'. Available positions: {medium_info}"
            )
        selected_medium: MBMedium | None = hits[0]
        log.info("disc_override_selected", position=disc_override)
    else:
        selected_medium = mediums[0] if mediums else None

        if len(mediums) > 1:
            selected_medium, selection_method = _select_medium_with_reason(
                mediums, len(src_files), src_dir.name, track_frames=track_frames, dtitle=dtitle
            )
            # When a heuristic (title or fallback) selected the medium, prompt for confirmation
            # unless we're in dry-run mode or no UI was provided.
            if selection_method in {SelectionMethod.TITLE, SelectionMethod.FALLBACK} and ui is not None and not dry_run:
                release_url = f"https://musicbrainz.org/release/{release_id}"
                confirmed = ui.confirm_disc(mediums, selected_medium, dtitle, release_url)
                if confirmed is None:
                    log.warning("disc_selection_aborted", release_id=release_id)
                    raise SystemExit(1)
                selected_medium = confirmed

    if selected_medium is None:
        raise ValueError(f"release '{release.title}' has no mediums")

    medium_pos = selected_medium.position

    # Build all_media_pairs: (MBTrack, medium_pos) for every track on every medium, in
    # medium-then-track order.  This is the aggregation surface for work-grouping and the three
    # unification passes; it spans all media of the release so movements of one work that straddle
    # a disc boundary are grouped correctly.  The global index 0..N_total-1 over this list is the
    # key for tags_map.
    all_media_pairs: list[tuple[MBTrack, int]] = [
        (track, medium.position) for medium in release.medium_list for track in medium.track_list
    ]
    log.info("release_tracks_total", count=len(all_media_pairs))

    # Build copy_subset: the list of (global_idx, MBTrack, medium_pos) for the selected medium
    # only, in track order.  The copy/tag/verify/journal loop operates exclusively on this subset,
    # preserving the single-medium copy semantics (P3).
    copy_subset: list[tuple[int, MBTrack, int]] = [
        (global_idx, track, med_pos) for global_idx, (track, med_pos) in enumerate(all_media_pairs) if med_pos == medium_pos
    ]
    log.info("release_tracks", count=len(copy_subset), disc=medium_pos)

    # Track-count mismatch check uses the copy-subset count (selected medium only), not the
    # all-media count, so the error message and guard remain scoped to the actioned medium.
    if len(src_files) != len(copy_subset):
        raise RuntimeError(
            f"track count mismatch for release '{release.title}': "
            f"{len(src_files)} source file(s) but {len(copy_subset)} track(s) on disc {medium_pos}"
        )

    # Duration pre-flight: warn when any source file's duration deviates from the corresponding
    # MB track length by more than 10 s.  Skipped in dry-run mode to preserve non-interactive
    # behaviour.  MB duration data is crowd-sourced and may be inaccurate for specific pressings,
    # so a mismatch is a warning requiring user confirmation rather than a hard error.
    # The preflight uses copy_subset track pairs (selected medium only).
    if not dry_run:
        copy_subset_track_pairs: list[tuple[MBTrack, int]] = [(track, med_pos) for _, track, med_pos in copy_subset]
        duration_warnings = check_duration_preflight(src_files, copy_subset_track_pairs)
        if duration_warnings:
            if not _prompt_duration_warnings(duration_warnings):
                raise SystemExit(1)

    # Fetch all cover art once for the whole release
    # Create the destination root if it does not exist yet.  All intermediate directories are also
    # created so that a fresh library path (e.g. /mnt/music/done) works on the first invocation
    # without requiring the user to pre-create it.  exist_ok=True makes this idempotent.
    if not dest_root.exists():
        dest_root.mkdir(parents=True, exist_ok=True)
        log.info("dest_root_created", path=str(dest_root))

    rg_id = release.release_group.id
    cover = CoverArt()
    if not dry_run:
        cover = fetch_cover_art(release_id, rg_id, no_cache=no_cache)
        if not cover.available:
            log.warning("cover_art_not_available", release_id=release_id)

    # tags_map: global_idx → TrackTags, keyed over all_media_pairs (all media).
    # This allows top_work_groups and the three unification passes to span disc boundaries.
    tags_map: dict[int, TrackTags] = {}

    if fetch_rels and not dry_run:
        log.info("fetch_recording_rels_start")
        for global_idx, (track, _med_pos) in enumerate(all_media_pairs):
            rec_id = track.recording.id
            log.info("fetch_recording", position=track.position, title=track.recording.title)

            rec_detail = fetch_recording_detail(rec_id)

            work_hierarchy: list[MBWork] = []
            # Inflate each performance-linked work stub to a full work before scoring.
            # _get_bottom_work fetches from MB only when the embedded work lacks relation data.
            performance_works = [
                _get_bottom_work(rel.work) for rel in rec_detail.work_relation_list if rel.type == "performance" and rel.work.id
            ]
            if performance_works:
                primary_work = select_primary_performance_work(performance_works)
                work_hierarchy = build_work_hierarchy(primary_work)

            tags_map[global_idx] = build_track_tags(release, track, _med_pos, rec_detail, work_hierarchy)
            tags_map[global_idx].acoustid_id = fetch_acoustid_id(rec_id)

        # Compute movement numbers grouped by top work MBID.
        # Iterates the full tags_map (all media) so movements of one work that straddle a disc
        # boundary are grouped together — this is the C-S0 contract.
        top_work_groups: dict[str, list[int]] = defaultdict(list)
        for global_idx in range(len(all_media_pairs)):
            t = tags_map[global_idx]
            twid = t.cwp_workid_top or t.musicbrainz_workid
            top_work_groups[twid].append(global_idx)

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

            # Enumerate intermediate sibling nodes at each hierarchy level i >= 1
            # (C-L1 contract).  This mirrors the leaf cwp_movt_num pass above but ranks
            # distinct sibling NODES (identified by cwp_workid_{i}) rather than tracks.
            # Siblings are nodes that share the same parent (cwp_workid_{i+1}).
            # Within each parent, nodes are ranked by ascending integer cwp_ordering_key_{i}
            # (non-digit values map to 0; ties broken by first-appearance order across
            # group_idxs).  The resulting gap-free, 1-based index is written to every track
            # belonging to that node as model_extra cwp_inter_index_{i}.
            # build_dest_path consumes CWP_INTER_INDEX_{i} for the intermediate directory
            # nn prefix, falling back to the raw ordering-key only when the index is absent
            # (no-group / no-hierarchy escape hatch).
            #
            # Collect the maximum intermediate level present in this group.
            max_inter_level = 0
            for grp_idx in group_idxs:
                extras = tags_map[grp_idx].model_extra or {}
                i = 1
                while f"cwp_workid_{i}" in extras:
                    max_inter_level = max(max_inter_level, i)
                    i += 1

            for i in range(1, max_inter_level + 1):
                # Collect distinct node ids at level i with their ordering-key and first
                # appearance order (for stable tie-breaking when ordering-keys collide).
                # node_id → (ordering_key_int, first_appearance_order)
                node_order: dict[str, tuple[int, int]] = {}
                # node_id → parent_id (cwp_workid_{i+1}, or "" when no parent level exists)
                node_parent: dict[str, str] = {}
                for appear_idx, grp_idx in enumerate(group_idxs):
                    extras = tags_map[grp_idx].model_extra or {}
                    node_id = extras.get(f"cwp_workid_{i}", "")
                    if not node_id:
                        continue
                    if node_id not in node_order:
                        ok_str = extras.get(f"cwp_ordering_key_{i}", "0")
                        ok_int = int(ok_str) if ok_str.isdigit() else 0
                        node_order[node_id] = (ok_int, appear_idx)
                        parent_id = extras.get(f"cwp_workid_{i + 1}", "")
                        node_parent[node_id] = parent_id

                if not node_order:  # pragma: no cover — guard for empty-workid data anomaly
                    continue

                # Group sibling node ids by parent; rank within each parent.
                # parents_nodes: parent_id → list of (ordering_key_int, appear_idx, node_id)
                parents_nodes: dict[str, list[tuple[int, int, str]]] = {}
                for node_id, (ok_int, appear_idx) in node_order.items():
                    parent_id = node_parent[node_id]
                    parents_nodes.setdefault(parent_id, []).append((ok_int, appear_idx, node_id))

                # Assign gap-free 1-based sibling index within each parent.
                node_sibling_index: dict[str, str] = {}
                for siblings in parents_nodes.values():
                    siblings.sort()  # ascending (ok_int, appear_idx) — stable tie-break
                    for rank, (_, _, node_id) in enumerate(siblings, start=1):
                        node_sibling_index[node_id] = str(rank)

                # Write the index back to every track belonging to each node.
                # Any non-empty node_id here was necessarily collected in node_order above, so
                # node_id in node_sibling_index is always true — just check node_id is non-empty.
                for grp_idx in group_idxs:
                    extras = tags_map[grp_idx].model_extra or {}
                    node_id = extras.get(f"cwp_workid_{i}", "")
                    if node_id:
                        extras[f"cwp_inter_index_{i}"] = node_sibling_index[node_id]

            # Unify cwp_composer_lastnames / cwp_composers across all movements of this
            # work.  When MB credits a completion or arranger as "composer" with the
            # "additional" attribute on only some movements, those movements have an empty
            # role_buckets.composers and fall back to additional_composers.
            # build_track_tags marks this case with cwp_composers_is_fallback="1".  The
            # result is a different CWP_COMPOSER_LASTNAMES — and therefore a different
            # top_dir — than the movements that carry a plain primary-composer relation.
            # Fix: propagate the primary-composer values from any movement that has them
            # (cwp_composers_is_fallback empty) to all movements that used the fallback,
            # so every movement in the group lands in the same top-level directory.
            #
            # group_idxs are global indices over all_media_pairs so this pass spans all
            # media of the release (C-S0 contract).
            _primary_cwp_composers = ""
            _primary_cwp_composers_sort = ""
            _primary_cwp_composer_lastnames = ""
            for grp_idx in group_idxs:
                t = tags_map[grp_idx]
                if t.cwp_composers and not t.cwp_composers_is_fallback:
                    _primary_cwp_composers = t.cwp_composers
                    _primary_cwp_composers_sort = t.cwp_composers_sort
                    _primary_cwp_composer_lastnames = t.cwp_composer_lastnames
                    break
            if _primary_cwp_composers:
                for grp_idx in group_idxs:
                    t = tags_map[grp_idx]
                    if t.cwp_composers_is_fallback:
                        t.cwp_composers = _primary_cwp_composers
                        t.cwp_composers_sort = _primary_cwp_composers_sort
                        t.cwp_composer_lastnames = _primary_cwp_composer_lastnames
                        t.cwp_composers_is_fallback = ""
                        t.composer = t.composer or _primary_cwp_composers
                        t.composersort = t.composersort or _primary_cwp_composers_sort
                        t.cea_composers = t.cea_composers or _primary_cwp_composers
                        t.cea_composer_lastnames = t.cea_composer_lastnames or _primary_cwp_composer_lastnames

            # Compute recording_date_work: the minimum interval spanning all movements of
            # this work across all media.  All tracks in the group use this unified value for
            # the destination directory label so movements recorded in different sessions land
            # in the same dir.  The per-track RECORDING_DATE tag is NOT modified — only this
            # path-construction helper is set.
            #
            # For each per-track RECORDING_DATE:
            #   - ISO interval "begin/end": contribute begin to _begins, end to _ends.
            #   - Single date "begin":      contribute begin to both _begins and _ends so that
            #     the max-end calculation captures the latest point-in-time session date.
            #
            # Result: _unified = min(_begins)/max(_ends) when the years differ, else min(_begins).
            _begins: list[str] = []
            _ends: list[str] = []
            for grp_idx in group_idxs:
                rd = tags_map[grp_idx].recording_date
                if not rd:
                    continue
                if "/" in rd:
                    b, _, e = rd.partition("/")
                    if b:  # pragma: no branch — begin is always non-empty for valid ISO intervals
                        _begins.append(b)
                        _ends.append(b)  # use begin as a floor for the end-side span
                    if e:  # pragma: no branch — end is always non-empty for valid ISO intervals
                        _ends.append(e)
                else:
                    _begins.append(rd)
                    _ends.append(rd)
            if _begins:
                _min_begin = min(_begins)
                _max_end = max(_ends)
                _unified = f"{_min_begin}/{_max_end}" if _max_end != _min_begin else _min_begin
                for grp_idx in group_idxs:
                    tags_map[grp_idx].recording_date_work = _unified

            # Normalize recording_first_release_date across all movements so the [rel YYYY]
            # fallback in build_dest_path is uniform when no session date is available.
            # This normalization only applies when recording_date_work is empty (i.e. no
            # movement in the group has a session date); if any movement has a session date
            # then recording_date_work is set and build_dest_path uses [rec …] for all
            # movements, bypassing the [rel] fallback entirely.
            #
            # recording_first_release_date is per-recording and can vary across movements
            # (e.g. a movement that first appeared on a different pressing year).  The release
            # date (release.date) is attached to the release itself and is therefore identical
            # for every track, making it the correct normalising source.  We use 4-digit year
            # precision only (matching [rel YYYY] output) and fall back to the release group's
            # first-release-date when release.date is absent.
            #
            # group_idxs are global indices over all_media_pairs so this pass spans all
            # media of the release (C-S0 contract).
            if not _begins:
                _rel_year = (release.date[:4] if len(release.date) >= 4 and release.date[:4].isdigit() else "") or (
                    release.release_group.first_release_date[:4]
                    if len(release.release_group.first_release_date) >= 4
                    and release.release_group.first_release_date[:4].isdigit()
                    else ""
                )
                if _rel_year:
                    for grp_idx in group_idxs:
                        if tags_map[grp_idx].recording_first_release_date:
                            tags_map[grp_idx].recording_first_release_date = _rel_year

            # Compute cea_album_soloists_unified: the cross-medium UNION of soloists for this
            # top work, written to every track in the group as a PATH-ONLY helper field (never
            # written to audio files — excluded in TrackTags.to_file_dict).
            #
            # Editorial rule: unified path components ACCUMULATE per work across media.  A
            # concerto whose movements feature different soloists on different discs should
            # collect ALL of them into the path so every movement lands in the same directory.
            # The per-track tag worldview is NOT changed — only this path helper accumulates.
            #
            # Source priority per track:
            #   1. cea_album_soloists (release-level soloist credit, most stable)
            #   2. cea_soloists       (per-track fallback when album-level is empty)
            #
            # Dedup is order-preserving (first-appearance order); "; " join, preserving any
            # instrument-in-parens suffix already present in the individual strings.
            #
            # group_idxs are global indices over all_media_pairs so this pass spans all
            # media of the release (C-S0 contract).
            _seen_soloists: set[str] = set()
            _union_soloists: list[str] = []
            for grp_idx in group_idxs:
                t = tags_map[grp_idx]
                source = t.cea_album_soloists or t.cea_soloists
                if source:
                    for _soloist_entry in source.split("; "):
                        _soloist_entry = _soloist_entry.strip()
                        if _soloist_entry and _soloist_entry not in _seen_soloists:
                            _seen_soloists.add(_soloist_entry)
                            _union_soloists.append(_soloist_entry)
            if _union_soloists:
                _unified_soloists = "; ".join(_union_soloists)
                for grp_idx in group_idxs:
                    tags_map[grp_idx].cea_album_soloists_unified = _unified_soloists

    else:
        # Minimal tags for every track on every medium (uniform map shape regardless of branch).
        # This ensures tags_map is always keyed 0..N_total-1 over all_media_pairs.
        label_info = release.label_info_list[0] if release.label_info_list else None
        for global_idx, (track, _med_pos) in enumerate(all_media_pairs):
            tags_map[global_idx] = TrackTags(
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

    # Build the copy plan from the copy_subset only (selected medium).
    # CopyPlanEntry.idx is the GLOBAL index into tags_map so tags_map[entry.idx] resolves
    # correctly even though the plan only covers the selected medium.
    # global_track_idx passed to build_dest_path is the 1-based enumeration over the copy
    # subset (not the all-media global index) to preserve today's per-run unique-filename
    # behaviour for the actioned medium.
    plan: list[CopyPlanEntry] = []
    for copy_subset_pos, (global_idx, track, _med_pos) in enumerate(copy_subset):
        src_file = src_files[copy_subset_pos]
        final_tags = tags_map[global_idx]
        # 1-based position within the copy subset — used as the leaf nn fallback when
        # CWP_ORDERING_KEY_0 is absent.  Scoped to the copy subset so filenames are
        # monotonically increasing within the actioned medium regardless of disc position.
        dest_base = build_dest_path(dest_root, release, track, final_tags, global_track_idx=copy_subset_pos + 1)
        dest_file = dest_base.with_suffix(src_file.suffix.lower())
        log.info("copy_track", src=src_file.name, dest=str(dest_file.relative_to(dest_root)))
        plan.append(CopyPlanEntry(idx=global_idx, src_file=src_file, dest_file=dest_file))

    # --- Name-length check and resolution ---
    # In dry-run mode: log warnings only.  Otherwise: prompt via UI or auto-shorten when no UI.
    if dry_run:
        _warn_long_names(plan, dest_root)
    else:
        plan = _resolve_long_names(plan, dest_root, ui)

    # --- Collision detection and resolution ---
    # Build (src, dest, acoustid, length_ms) tuples for _assess_collisions.
    plan_pairs: list[tuple[Path, Path, str, int]] = []
    for entry in plan:
        t = tags_map[entry.idx]
        try:
            length_ms = int(t.length) if t.length else 0
        except ValueError:
            length_ms = 0
        plan_pairs.append((entry.src_file, entry.dest_file, t.acoustid_id, length_ms))

    skip_dest: set[Path] = set()
    if not dry_run:
        collision_results = _assess_collisions(plan_pairs)
        if collision_results:
            confirmed_matches = [r for r in collision_results if r.match is True]
            confirmed_nonmatches = [r for r in collision_results if r.match is False]
            inconclusive = [r for r in collision_results if r.match is None]

            # Non-matches: automatically rewrite the incoming path with a release suffix so both
            # recordings coexist.  No user prompt is required — log a warning per affected track.
            if confirmed_nonmatches:
                _apply_collision_suffix(plan, confirmed_nonmatches, release, dest_root)
                log.warning("collision_nonmatch_auto_suffix", count=len(confirmed_nonmatches))

            # Identical and inconclusive collisions: present to user with comparison context.
            prompt_results = confirmed_matches + inconclusive
            if prompt_results:
                policy = collision_policy
                if policy == CollisionPolicy.ASK:
                    policy = _prompt_collision_policy(prompt_results, dest_root)
                prompt_dests = {r.dest for r in prompt_results}
                match policy:
                    case CollisionPolicy.OVERWRITE:
                        log.info("collision_overwrite", count=len(prompt_results))
                    case CollisionPolicy.SKIP:
                        skip_dest = prompt_dests
                        log.info("collision_skip", skipped=len(skip_dest))
                    case CollisionPolicy.ABORT:
                        log.warning("collision_abort")
                        raise SystemExit(1)
                    case _:  # pragma: no cover
                        pass

    # --- Copy, tag, and journal ---
    journal_entries: list[TransactionEntry] = []
    sidecars_written: set[Path] = set()
    freedb_written: set[Path] = set()
    now = datetime.datetime.now(datetime.UTC).isoformat()

    for entry in plan:
        idx, src_file, dest_file = entry.idx, entry.src_file, entry.dest_file
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

        # Compute the tagging-invariant audio hash from the source file before tagging mutates
        # the destination.  The source and destination bytes are identical at this point (the raw
        # copy integrity check above has already passed), so hashing the source is equivalent to
        # hashing the pre-tag destination while avoiding any timing dependency on the copy.
        final_tags.audio_hash = _audio_hash(src_file)

        # Compute the Chromaprint fingerprint from the source file before tagging.  Returns ""
        # when fpcalc is not available; the empty string is stored as-is (no special-casing).
        # Mirrors the F1 pattern for audio_hash: computed on the source before apply_tags_*.
        final_tags.chromaprint_fp = _run_fpcalc(src_file)

        # AcoustID identity-confirm: when an API key is supplied and a fingerprint is available,
        # look up the recording MBIDs for this file and check whether the selected recording MBID
        # is among them.  This is a read-only diagnostic step — it never alters the copy/tag/verify
        # path, never raises, and never blocks on empty results.
        if acoustid_key and final_tags.chromaprint_fp:
            _confirm_dur_s = _read_duration_ms(src_file) // 1000
            _confirm_mbids = fetch_acoustid_lookup(final_tags.chromaprint_fp, _confirm_dur_s, acoustid_key)
            _selected_rec_id = final_tags.musicbrainz_recordingid
            if _confirm_mbids and _selected_rec_id:
                if _selected_rec_id in _confirm_mbids:
                    log.info(
                        "acoustid_confirm_ok",
                        recording_id=_selected_rec_id,
                        src=src_file.name,
                    )
                else:
                    log.warning(
                        "acoustid_confirm_mismatch",
                        recording_id=_selected_rec_id,
                        src=src_file.name,
                        acoustid_top=_confirm_mbids[0] if _confirm_mbids else "",
                    )

        # Set cover art sidecar reference tags so they are embedded in the audio file.
        def _filenames(images: list[CoverImage]) -> str:
            """Return unique semicolon-joined filenames from a list of CoverImages.

            Deduplicates filenames so multi-type images shared between buckets appear only once.

            :param images: List of :class:`~music_annotator.models.CoverImage` instances.
            :returns: Semicolon-joined unique non-empty filename strings.
            """
            seen: set[str] = set()
            parts: list[str] = []
            for img in images:
                if img.filename and img.filename not in seen:
                    seen.add(img.filename)
                    parts.append(img.filename)
            return "; ".join(parts)

        final_tags.coverart_front_file = cover.front_full[0].filename if cover.front_full else ""
        final_tags.coverart_back_file = _filenames(cover.back)
        final_tags.coverart_booklet_files = _filenames(cover.booklet)
        final_tags.coverart_medium_files = _filenames(cover.medium)
        final_tags.coverart_tray_files = _filenames(cover.tray)
        final_tags.coverart_obi_files = _filenames(cover.obi)
        final_tags.coverart_spine_files = _filenames(cover.spine)
        final_tags.coverart_track_files = _filenames(cover.track)
        final_tags.coverart_liner_files = _filenames(cover.liner)
        final_tags.coverart_sticker_files = _filenames(cover.sticker)
        final_tags.coverart_poster_files = _filenames(cover.poster)
        final_tags.coverart_matrix_files = _filenames(cover.matrix)
        final_tags.coverart_top_files = _filenames(cover.top)
        final_tags.coverart_bottom_files = _filenames(cover.bottom)
        final_tags.coverart_panel_files = _filenames(cover.panel)
        final_tags.coverart_watermark_files = _filenames(cover.watermark)
        final_tags.coverart_raw_files = _filenames(cover.raw)
        final_tags.coverart_other_files = _filenames(cover.other)
        final_tags.coverart_unknown_files = _filenames(cover.unknown)

        ext = src_file.suffix.lower()
        try:
            match ext:
                case ".flac":
                    apply_tags_flac(dest_file, final_tags, cover)
                case ".mp3":
                    apply_tags_mp3(dest_file, final_tags, cover)
                case _:
                    log.warning("unsupported_format", ext=ext, file=dest_file.name)
        except MutagenError as exc:
            raise RuntimeError(f"tag write failure for '{dest_file.name}': {exc}") from exc

        os.utime(dest_file, src_times)

        _verify_copy(src_file, dest_file, final_tags, cover, src_stat.st_mtime)

        # Derive the work top directory (dest_root / composer-dir / work-dir) and write
        # sidecar cover art files exactly once per work directory across all tracks.
        rel_parts = dest_file.relative_to(dest_root).parts
        work_top_dir = dest_root / rel_parts[0] / rel_parts[1]
        _write_sidecars(cover, work_top_dir, sidecars_written, journal_entries, now, release_id)
        _write_freedb_yaml(src_dir, work_top_dir, medium_pos, freedb_written, journal_entries, now, release_id)

        journal_entries.append(
            TransactionEntry(
                timestamp=now,
                release_id=release_id,
                source=str(src_file),
                destination=str(dest_file),
                action="tagged",
                audio_hash=final_tags.audio_hash,
                chromaprint_fp=final_tags.chromaprint_fp,
            )
        )

    if not dry_run:
        write_transaction_log(dest_root / JOURNAL_FILENAME, journal_entries)

        # Count copied (not skipped/dry-run) entries and print a confirmation message so the user
        # knows it is safe to delete the source directory before they do so.
        copied = [e for e in journal_entries if e.action == "tagged"]
        dest_dirs = sorted(
            {
                Path(e.destination).relative_to(dest_root).parts[0] / Path(Path(e.destination).relative_to(dest_root).parts[1])
                for e in copied
            }
        )
        if copied:
            _console.print(
                f"\n[bold green]Verified OK:[/] [green]{len(copied)} file(s) written and confirmed under "
                f"{_markup_escape(str(dest_root))}:[/]"
            )
            for d in dest_dirs:
                _console.print(f"  [green]{_markup_escape(str(d))}[/]")
            _console.print("[bold green]It is safe to delete the source directory.[/]\n")

    log.info("run_complete", dest=str(dest_root))


def _tags_from_file_dict(file_dict: dict[str, str]) -> TrackTags:
    """Reconstruct a :class:`~music_annotator.models.TrackTags` instance from an on-disk tag dict.

    Reads all uppercase tag keys produced by :func:`~music_annotator._pipeline_io._read_tags_flac`
    or :func:`~music_annotator._pipeline_io._read_tags_mp3` and populates a :class:`TrackTags`
    instance so that calling ``to_file_dict()`` on the result produces exactly the same uppercase
    dict.

    Named :class:`TrackTags` fields are populated by lowercasing the key and passing as keyword
    arguments via ``model_validate``.  Dynamic per-level fields (``CWP_WORK_0``, ``CWP_WORKID_0``,
    ``CWP_PART_0``, ``CWP_INTER_INDEX_1``, etc.) that are not named fields are placed in
    ``model_extra`` as lowercase keys so that ``to_file_dict()`` includes them via its extras loop.

    :param file_dict: Uppercase ``{KEY: value}`` mapping read back from an audio file.
    :returns: A :class:`TrackTags` with all tags populated, suitable for passing to
        :func:`~music_annotator._pipeline_io._verify_copy`.
    """
    # Split into named fields (known to TrackTags) vs dynamic extras
    named: dict[str, str] = {}
    extras: dict[str, str] = {}

    # Build set of known field names (lowercase) from the model
    known_fields: frozenset[str] = frozenset(TrackTags.model_fields)
    # Guard against the two fields that ARE named model fields but must not be round-tripped:
    # - recording_date_work: an in-memory path-construction helper, never written to audio files.
    # - cwp_composers_is_fallback: an internal flag used only during the ingest pipeline pass.
    # The other to_file_dict() exclusions (cea_*_list fields) are list-typed model fields and
    # will never appear in a read-back tag dict (they are not written to audio files), so they
    # do not need to be explicitly excluded here.
    _excluded = frozenset({"recording_date_work", "cwp_composers_is_fallback"})

    for key, value in file_dict.items():
        lower_key = key.lower()
        if lower_key in known_fields and lower_key not in _excluded:
            named[lower_key] = value
        else:
            # Dynamic per-level field (e.g. cwp_work_0, cwp_workid_0, cwp_inter_index_1) —
            # store with lowercase key so to_file_dict() uppercases it correctly.
            extras[lower_key] = value

    tags = TrackTags.model_validate(named)
    # Merge extras into model_extra (Pydantic's extra="allow" dict).
    # With extra="allow", model_extra is always a dict (never None) after model_validate —
    # the None branch is a defensive guard that cannot be reached in practice.
    if tags.model_extra is None:  # pragma: no cover
        object.__setattr__(tags, "__pydantic_extra__", extras)
    else:
        tags.model_extra.update(extras)
    return tags


def repath(dest_root: Path, *, dry_run: bool = False) -> None:
    """Re-path all verified library files under ``dest_root`` to their corrected destinations.

    Walks the already-annotated library at ``dest_root``, reads the transaction journal to
    identify verified library files (``action in {"tagged", "repathed"}``), recomputes each
    file's destination path from its **embedded tags alone** (no MusicBrainz network calls),
    and moves files whose current path differs from the recomputed path.

    This maintenance-mode command is the retroactive counterpart to a path-policy change (such as
    the L0/L1 leaf and intermediate-directory numbering fix): it brings an already-annotated library
    forward to the new policy without re-ingesting from source.

    **Move semantics (provenance-chain invariant preserved):** for each file that needs moving:

    1. Capture source SHA-256.
    2. Move atomically via ``os.replace`` (rename within the library); fall back to
       ``shutil.copy2`` + ``os.unlink`` on ``OSError`` with ``errno.EXDEV`` (cross-filesystem).
    3. Verify destination SHA-256 == source SHA-256 (``RuntimeError`` on mismatch — NO journal
       entry written).
    4. Run ``_verify_copy`` tag round-trip on the new path (``RuntimeError`` on mismatch — NO
       journal entry written).
    5. **Only then** append ``TransactionEntry(action="repathed", source=<old path>,
       destination=<new path>)`` and flush it to the journal before moving the next file, so a
       crash leaves a complete audit trail.

    No-op: files whose recomputed path matches their current path are skipped silently.

    Collision: when two legacy paths recompute to the same new path, the same
    ``_assess_collisions`` / ``_apply_collision_suffix`` machinery used by :func:`run` is applied
    (acoustid+length-aware, single collision authority).

    In ``dry_run`` mode: all planned moves and collisions are logged but **no files are moved
    and no journal entries are written**.

    .. warning::
        A bare ``repath <dest>`` invocation **mass-relocates the entire library**.  The
        ``action="repathed"`` journal entries are the complete recovery record — if something goes
        wrong, examine ``music_annotator_journal.json`` in ``dest_root`` to reconstruct what
        moved where.  Use ``--dry-run`` first to preview all planned moves.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param dry_run: When ``True``, log planned moves without performing any filesystem
        operations or writing journal entries.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = read_journal(journal_path)

    # --- Determine the current canonical path for each logical file ---
    # A "repathed" entry supersedes an earlier "tagged" entry for the same logical file.
    # Track the latest destination per original source lineage: use the destination of the
    # latest entry per (source -> latest dest) chain.
    # Strategy: walk entries in order, building a map from each destination -> (source_lineage).
    # A "repathed" entry's source is the OLD path = a previous destination.
    # We want: for each current on-disk file, what was the original ingest source?
    # Use a union-find-like approach: dest_to_current maps each old dest to the latest dest
    # in its chain.

    # Build the set of current library file paths from journal.
    # dest_to_lineage_source: current_dest -> original ingest source
    # last_dest_for_source: original_source -> latest_dest
    current_lib: dict[Path, str] = {}  # current_path -> original_source (for journal release_id lookup)

    for entry in journal.entries:
        if entry.action not in {"tagged", "repathed"}:
            continue
        dest_path = Path(entry.destination)
        source_path = entry.source

        if entry.action == "tagged":
            # This is an ingest entry; source = original ingest path
            # Check if this destination was later repathed (it will appear as source of a later
            # repathed entry).  For now, register it; a later "repathed" entry will update.
            current_lib[dest_path] = source_path
        else:
            # action == "repathed": source = old path, destination = new path
            old_path = Path(entry.source)
            # Remove the old path from current_lib (it moved)
            current_lib.pop(old_path, None)
            # Register the new path — carry the original source lineage info from old if available
            original_src = current_lib.get(old_path, source_path)
            current_lib[dest_path] = original_src

    # Re-walk to carry forward lineage for multi-hop repath chains
    # (repathed entry's source may itself have been repathed — iterate until stable)
    # Actually the above loop handles it correctly because we process entries in order:
    # each "repathed" entry pops the old path and registers the new one, so multi-hop chains
    # naturally resolve as long as we process in chronological order (which we do).

    # Filter to files that actually exist on disk
    existing_files: list[Path] = [p for p in current_lib if p.exists()]

    if not existing_files:
        log.info("repath_nothing_to_move", dest_root=str(dest_root))
        return

    # --- Build repath plan: (current_path, new_dest, acoustid, length_ms) ---
    plan_pairs: list[tuple[Path, Path, str, int]] = []

    for current_path in existing_files:
        ext = current_path.suffix.lower()
        try:
            match ext:
                case ".flac":
                    file_dict = _read_tags_flac(current_path)
                case ".mp3":
                    file_dict = _read_tags_mp3(current_path)
                case _:  # pragma: no cover — AUDIO_EXTENSIONS may include unsupported types
                    log.warning("repath_unsupported_format", path=str(current_path), ext=ext)
                    continue
        except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
            log.warning("repath_tag_read_error", path=str(current_path), error=str(exc))
            continue

        tags = _tags_from_file_dict(file_dict)

        # Construct minimal stand-in objects for build_dest_path.
        # build_dest_path reads release.artist_credit only when CWP_COMPOSER_LASTNAMES and
        # CEA_COMPOSER_LASTNAMES are both absent — an edge case that defaults gracefully to
        # "Unknown Composer".  track.position is used only as the deepest leaf-nn fallback
        # (when CWP_MOVT_NUM is absent and global_track_idx=0); zero is acceptable here
        # because CWP_MOVT_NUM must be present for the repath to produce a meaningful path.
        stub_release = MBRelease()
        stub_track = MBTrack()

        new_dest_base = build_dest_path(dest_root, stub_release, stub_track, tags, global_track_idx=0)
        new_dest = new_dest_base.with_suffix(ext)

        if new_dest == current_path:
            log.debug("repath_noop", path=str(current_path.relative_to(dest_root)))
            continue

        acoustid = file_dict.get("ACOUSTID_ID", "")
        length_str = file_dict.get("LENGTH", "0")
        try:
            length_ms = int(length_str) if length_str else 0
        except ValueError:
            length_ms = 0

        plan_pairs.append((current_path, new_dest, acoustid, length_ms))
        log.info(
            "repath_plan",
            old=str(current_path.relative_to(dest_root)),
            new=str(new_dest.relative_to(dest_root)),
            dry_run=dry_run,
        )

    if not plan_pairs:
        log.info("repath_all_current", dest_root=str(dest_root))
        return

    # --- Collision detection and resolution ---
    collision_results = _assess_collisions(plan_pairs)
    confirmed_nonmatches = [r for r in collision_results if r.match is False]
    if confirmed_nonmatches:
        # Rewrite destinations for confirmed non-matches using a release-stub suffix.
        # _apply_collision_suffix expects a list[CopyPlanEntry]; build temporary stubs using
        # CopyPlanEntry (src_file=current_path, dest_file=new_dest, idx=0).
        stub_plan = [CopyPlanEntry(idx=0, src_file=src, dest_file=dest) for src, dest, _, _ in plan_pairs]
        stub_release_for_suffix = MBRelease()
        _apply_collision_suffix(stub_plan, confirmed_nonmatches, stub_release_for_suffix, dest_root)
        # Rebuild plan_pairs with updated destinations
        plan_pairs = [
            (entry.src_file, entry.dest_file, acust, length) for entry, (_, _, acust, length) in zip(stub_plan, plan_pairs)
        ]
        log.warning("repath_collision_suffix_applied", count=len(confirmed_nonmatches))

    if dry_run:
        for current_path, new_dest, _, _ in plan_pairs:
            log.info(
                "repath_dry_run",
                old=str(current_path.relative_to(dest_root)),
                new=str(new_dest.relative_to(dest_root)),
            )
        return

    # --- Perform moves, verify, journal ---
    now = datetime.datetime.now(datetime.UTC).isoformat()
    for current_path, new_dest, _, _ in plan_pairs:
        # a. Capture source SHA-256 and mtime before the move
        src_hash = _sha256_file(current_path)
        src_stat = current_path.stat()
        src_mtime = src_stat.st_mtime

        # b. Ensure parent directory exists; move atomically
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(current_path, new_dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # Cross-filesystem fallback: copy + verify + unlink
            shutil.copy2(current_path, new_dest)
            # Verify the copy before unlinking the source
            cross_hash = _sha256_file(new_dest)
            if cross_hash != src_hash:
                new_dest.unlink(missing_ok=True)
                raise RuntimeError(
                    f"cross-fs copy integrity failure for '{current_path.name}': "
                    f"src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {cross_hash[:12]}…"
                ) from exc
            os.unlink(current_path)

        # c. Verify destination SHA-256 == source SHA-256
        dest_hash = _sha256_file(new_dest)
        if dest_hash != src_hash:
            raise RuntimeError(
                f"repath integrity failure for '{new_dest.name}': src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {dest_hash[:12]}…"
            )

        # d. Reconstruct tags for _verify_copy (tags are unchanged by the move)
        ext = current_path.suffix.lower()
        try:
            match ext:
                case ".flac":
                    post_dict = _read_tags_flac(new_dest)
                case ".mp3":
                    post_dict = _read_tags_mp3(new_dest)
                case _:  # pragma: no cover
                    post_dict = {}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"repath tag re-read failure for '{new_dest.name}': {exc}") from exc
        moved_tags = _tags_from_file_dict(post_dict)
        # _verify_copy checks mtime; for os.replace (same-fs rename) mtime is preserved.
        # For cross-fs copy2+unlink, shutil.copy2 copies atime/mtime so src_mtime still holds.
        _verify_copy(current_path, new_dest, moved_tags, None, src_mtime)

        # e. Journal the move and flush before proceeding to the next file
        entry = TransactionEntry(
            timestamp=now,
            release_id="",
            source=str(current_path),
            destination=str(new_dest),
            action="repathed",
        )
        write_transaction_log(journal_path, [entry])
        log.info(
            "repath_moved",
            old=str(current_path.relative_to(dest_root)),
            new=str(new_dest.relative_to(dest_root)),
        )

        # Clean up now-empty source directories (best-effort; non-empty dirs are skipped)
        src_dir = current_path.parent
        while src_dir != dest_root:
            try:
                src_dir.rmdir()  # Only succeeds if directory is now empty
                log.info("repath_removed_empty_dir", dir=str(src_dir.relative_to(dest_root)))
                src_dir = src_dir.parent
            except OSError:
                break

    log.info("repath_complete", dest_root=str(dest_root), moved=len(plan_pairs))


def regroup(dest_root: Path, *, yes: bool = False, dry_run: bool = False) -> None:
    """Consolidate confirmed split-release files into their canonical destinations.

    Reads the transaction journal, runs the S7 fragmentation-confirmation audit
    (:func:`~music_annotator._pipeline_io._confirm_fragmentation`), and acts on **confirmed
    case-(b) split-release candidates only** — release MBIDs whose tracks are scattered across
    more than one work directory, where at least one backing file's embedded ``MUSICBRAINZ_ALBUMID``
    tag confirms the journal's ``release_id`` (i.e. ``confirmed=True``).

    For each confirmed split release the affected files are identified by filtering ``action ==
    "tagged"`` journal entries whose ``release_id`` is in the confirmed set.  Each file's canonical
    destination is recomputed from its **embedded tags alone** via :func:`~music_annotator.build_dest_path`
    (the same offline engine :func:`repath` uses — no MusicBrainz network calls).

    **Move semantics (provenance-chain invariant preserved):** for each file that needs moving:

    1. Capture source SHA-256.
    2. Move atomically via ``os.replace`` (rename within the library); fall back to
       ``shutil.copy2`` + ``os.unlink`` on ``OSError`` with ``errno.EXDEV`` (cross-filesystem).
    3. Verify destination SHA-256 == source SHA-256 (``RuntimeError`` on mismatch — NO journal
       entry written).
    4. Run ``_verify_copy`` tag round-trip on the new path (``RuntimeError`` on mismatch — NO
       journal entry written).
    5. **Only then** append ``TransactionEntry(action="regrouped", release_id=<the split
       release's MBID>, source=<old path>, destination=<new path>)`` and flush it to the journal
       before moving the next file.

    Unlike :func:`repath`, the ``release_id`` field is populated in ``"regrouped"`` entries
    because the move is release-driven: the release MBID that drove candidate selection is already
    known from the journal and is recorded so that future audits can re-confirm the entry without a
    MusicBrainz lookup.  This keeps the regrouped entry self-describing and preserves P2
    (journal detects, tag adjudicates) for the regroup path.

    Collision: when two files recompute to the same new path, the same
    ``_assess_collisions`` / ``_apply_collision_suffix`` machinery used by :func:`run` is applied.

    In ``dry_run`` mode: all planned moves are logged but **no files are moved and no journal
    entries are written**.  The confirmation prompt is not shown in dry-run mode.

    When ``yes=True`` the confirmation prompt is skipped; files are moved immediately after
    building the plan.  When ``yes=False`` (default), the planned moves are printed and the user
    must confirm with ``y``/``yes`` before any move is performed.  When the plan is empty, a
    "nothing to regroup" message is logged and the function returns immediately.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param yes: When ``True``, skip the interactive confirmation prompt.
    :param dry_run: When ``True``, log planned moves without performing any filesystem
        operations or writing journal entries.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = read_journal(journal_path)

    # --- Identify confirmed case-(b) split-release candidates ---
    # _confirm_fragmentation returns (case_a, case_b); we act on case_b only.
    _, case_b = _confirm_fragmentation(dest_root, journal)
    confirmed_release_ids: set[str] = {rid for rid, (_, confirmed) in case_b.items() if confirmed}

    if not confirmed_release_ids:
        log.info("regroup_nothing_to_regroup", dest_root=str(dest_root))
        return

    # --- Identify affected files from journal entries ---
    # Map each confirmed release_id to the current on-disk path of its "tagged" entries.
    # We need to resolve the current path, accounting for any subsequent "repathed" moves.
    # Build current_lib: current_path -> (original_release_id) for confirmed releases,
    # mirroring repath's lineage-tracking approach for consistency.
    #
    # Strategy: walk all entries in order.  "tagged" entries for confirmed release_ids seed
    # the map.  "repathed" entries update the current location of any path that moved.
    current_lib: dict[Path, str] = {}  # current_path -> release_id

    for entry in journal.entries:
        dest_path = Path(entry.destination)
        if entry.action == "tagged" and entry.release_id in confirmed_release_ids:
            current_lib[dest_path] = entry.release_id
        elif entry.action in {"repathed", "regrouped"}:
            old_path = Path(entry.source)
            if old_path in current_lib:
                release_id_for_path = current_lib.pop(old_path)
                current_lib[dest_path] = release_id_for_path
        elif entry.action == "enriched":
            # In-place update: source == destination, path unchanged.
            # Re-register to keep release_id current for confirmed release IDs.
            if dest_path in current_lib:
                current_lib[dest_path] = entry.release_id

    # Filter to files that actually exist on disk
    existing_files: list[tuple[Path, str]] = [(p, rid) for p, rid in current_lib.items() if p.exists()]

    if not existing_files:
        log.info("regroup_nothing_to_regroup", dest_root=str(dest_root))
        return

    # --- Build regroup plan: (current_path, new_dest, acoustid, length_ms, release_id) ---
    plan_pairs: list[tuple[Path, Path, str, int, str]] = []

    for current_path, release_id in existing_files:
        ext = current_path.suffix.lower()
        try:
            match ext:
                case ".flac":
                    file_dict = _read_tags_flac(current_path)
                case ".mp3":
                    file_dict = _read_tags_mp3(current_path)
                case _:  # pragma: no cover — AUDIO_EXTENSIONS may include unsupported types
                    log.warning("regroup_unsupported_format", path=str(current_path), ext=ext)
                    continue
        except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
            log.warning("regroup_tag_read_error", path=str(current_path), error=str(exc))
            continue

        tags = _tags_from_file_dict(file_dict)

        stub_release = MBRelease()
        stub_track = MBTrack()

        new_dest_base = build_dest_path(dest_root, stub_release, stub_track, tags, global_track_idx=0)
        new_dest = new_dest_base.with_suffix(ext)

        if new_dest == current_path:
            log.debug("regroup_noop", path=str(current_path.relative_to(dest_root)))
            continue

        acoustid = file_dict.get("ACOUSTID_ID", "")
        length_str = file_dict.get("LENGTH", "0")
        try:
            length_ms = int(length_str) if length_str else 0
        except ValueError:
            length_ms = 0

        plan_pairs.append((current_path, new_dest, acoustid, length_ms, release_id))
        log.info(
            "regroup_plan",
            old=str(current_path.relative_to(dest_root)),
            new=str(new_dest.relative_to(dest_root)),
            release_id=release_id,
            dry_run=dry_run,
        )

    if not plan_pairs:
        log.info("regroup_nothing_to_regroup", dest_root=str(dest_root))
        return

    # --- Collision detection and resolution ---
    collision_pairs = [(src, dest, acust, length) for src, dest, acust, length, _ in plan_pairs]
    collision_results = _assess_collisions(collision_pairs)
    confirmed_nonmatches = [r for r in collision_results if r.match is False]
    if confirmed_nonmatches:
        stub_plan = [CopyPlanEntry(idx=0, src_file=src, dest_file=dest) for src, dest, _, _, _ in plan_pairs]
        stub_release_for_suffix = MBRelease()
        _apply_collision_suffix(stub_plan, confirmed_nonmatches, stub_release_for_suffix, dest_root)
        plan_pairs = [
            (entry.src_file, entry.dest_file, acust, length, rid)
            for entry, (_, _, acust, length, rid) in zip(stub_plan, plan_pairs)
        ]
        log.warning("regroup_collision_suffix_applied", count=len(confirmed_nonmatches))

    if dry_run:
        for current_path, new_dest, _, _, release_id in plan_pairs:
            log.info(
                "regroup_dry_run",
                old=str(current_path.relative_to(dest_root)),
                new=str(new_dest.relative_to(dest_root)),
                release_id=release_id,
            )
        return

    # --- Confirmation prompt ---
    if not yes:
        _console.print("\n[bold yellow]regroup[/] will move the following files:\n")
        for current_path, new_dest, _, _, release_id in plan_pairs:
            _console.print(
                f"  [dim]{_markup_escape(str(current_path.relative_to(dest_root)))}[/]\n"
                f"    → [green]{_markup_escape(str(new_dest.relative_to(dest_root)))}[/]"
                f"  [dim](release {_markup_escape(release_id)})[/]"
            )
        _console.print(f"\n[bold]{len(plan_pairs)} file(s) will be moved.[/]  Proceed? [dim](y/n)[/]")
        _console.print("\n[bold cyan]>[/] ", end="")
        answer = input("").strip().lower()
        if answer not in {"y", "yes"}:
            log.info("regroup_aborted", dest_root=str(dest_root))
            return

    # --- Perform moves, verify, journal ---
    now = datetime.datetime.now(datetime.UTC).isoformat()
    for current_path, new_dest, _, _, release_id in plan_pairs:
        # a. Capture source SHA-256 and mtime before the move
        src_hash = _sha256_file(current_path)
        src_stat = current_path.stat()
        src_mtime = src_stat.st_mtime

        # b. Ensure parent directory exists; move atomically
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(current_path, new_dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # Cross-filesystem fallback: copy + verify + unlink
            shutil.copy2(current_path, new_dest)
            # Verify the copy before unlinking the source
            cross_hash = _sha256_file(new_dest)
            if cross_hash != src_hash:
                new_dest.unlink(missing_ok=True)
                raise RuntimeError(
                    f"cross-fs copy integrity failure for '{current_path.name}': "
                    f"src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {cross_hash[:12]}…"
                ) from exc
            os.unlink(current_path)

        # c. Verify destination SHA-256 == source SHA-256
        dest_hash = _sha256_file(new_dest)
        if dest_hash != src_hash:
            raise RuntimeError(
                f"regroup integrity failure for '{new_dest.name}': "
                f"src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {dest_hash[:12]}…"
            )

        # d. Reconstruct tags for _verify_copy (tags are unchanged by the move)
        ext = current_path.suffix.lower()
        try:
            match ext:
                case ".flac":
                    post_dict = _read_tags_flac(new_dest)
                case ".mp3":
                    post_dict = _read_tags_mp3(new_dest)
                case _:  # pragma: no cover
                    post_dict = {}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"regroup tag re-read failure for '{new_dest.name}': {exc}") from exc
        moved_tags = _tags_from_file_dict(post_dict)
        # _verify_copy checks mtime; for os.replace (same-fs rename) mtime is preserved.
        # For cross-fs copy2+unlink, shutil.copy2 copies atime/mtime so src_mtime still holds.
        _verify_copy(current_path, new_dest, moved_tags, None, src_mtime)

        # e. Journal the move with the release_id (unlike repath, which uses release_id="").
        # Recording the release_id keeps the entry self-describing: future audits can re-confirm
        # without a MusicBrainz lookup, preserving P2 (journal detects, tag adjudicates).
        entry = TransactionEntry(
            timestamp=now,
            release_id=release_id,
            source=str(current_path),
            destination=str(new_dest),
            action="regrouped",
        )
        write_transaction_log(journal_path, [entry])
        log.info(
            "regroup_moved",
            old=str(current_path.relative_to(dest_root)),
            new=str(new_dest.relative_to(dest_root)),
            release_id=release_id,
        )

        # Clean up now-empty source directories (best-effort; non-empty dirs are skipped)
        src_dir = current_path.parent
        while src_dir != dest_root:
            try:
                src_dir.rmdir()  # Only succeeds if directory is now empty
                log.info("regroup_removed_empty_dir", dir=str(src_dir.relative_to(dest_root)))
                src_dir = src_dir.parent
            except OSError:
                break

    log.info("regroup_complete", dest_root=str(dest_root), moved=len(plan_pairs))


def unify(dest_root: Path, *, yes: bool = False, dry_run: bool = False) -> None:
    """Consolidate performer-split fragmented releases into their canonical top_dirs.

    Scans ``dest_root`` for releases whose tracks are spread across ≥2 distinct top_dirs due to
    per-track ``CEA_SOLOISTS`` variation (the dominant N1 shape: 29 releases in the 2026-06 audit).
    For each fragmented release, reads the embedded tags from all its files, runs
    :func:`~music_annotator._tags.build_dest_path` over the full release group to compute the
    canonical destination for every file, and moves files that are not already at their canonical
    path.

    **Detection (C-W2):** a release is fragmented when ≥2 distinct top_dirs share the same
    ``MUSICBRAINZ_ALBUMID`` tag.  The join key is the embedded tag, not the journal.

    **Canonical path algorithm (C-W2):** :func:`~music_annotator._tags.build_dest_path` already
    computes the correct unified path when given all tracks of the release as a group, because the
    cross-medium composer pass and ``recording_date_work`` pass run over the full release group.
    The unified performer credit comes from the ``cea_album_soloists_unified`` field, which
    accumulates the cross-medium union of ``CEA_SOLOISTS`` (C-S4 concerto-soloist rule).

    **Move semantics (provenance-chain invariant preserved):** for each file that needs moving:

    1. Capture source SHA-256.
    2. Move atomically via ``os.replace`` (rename within the library); fall back to
       ``shutil.copy2`` + ``os.unlink`` on ``OSError`` with ``errno.EXDEV`` (cross-filesystem).
    3. Verify destination SHA-256 == source SHA-256 (``RuntimeError`` on mismatch — NO journal
       entry written).
    4. Run ``_verify_copy`` tag round-trip on the new path (``RuntimeError`` on mismatch — NO
       journal entry written).
    5. **Only then** append ``TransactionEntry(action="unified", release_id=<the release's MBID>,
       source=<old path>, destination=<new path>)`` and flush it to the journal before moving the
       next file.

    In ``dry_run`` mode: all planned moves are logged but **no files are moved and no journal
    entries are written**.  The confirmation prompt is not shown in dry-run mode.

    When ``yes=True`` the confirmation prompt is skipped; files are moved immediately after
    building the plan.  When ``yes=False`` (default), the planned moves are printed and the user
    must confirm with ``y``/``yes`` before any move is performed.  When the plan is empty, a
    "nothing to unify" message is logged and the function returns immediately.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param yes: When ``True``, skip the interactive confirmation prompt.
    :param dry_run: When ``True``, log planned moves without performing any filesystem
        operations or writing journal entries.
    """
    journal_path = dest_root / JOURNAL_FILENAME

    # --- Detect fragmented releases by scanning embedded MUSICBRAINZ_ALBUMID tags ---
    fragmented = detect_fragmented_releases(dest_root)

    if not fragmented:
        log.info("unify_nothing_to_unify", dest_root=str(dest_root))
        return

    # --- Build unify plan: (current_path, new_dest, acoustid, length_ms, release_id) ---
    plan_pairs: list[tuple[Path, Path, str, int, str]] = []

    for release_id, file_paths in sorted(fragmented.items()):
        # Read tags from all files in this release group.
        # Build a list of (file_path, tags, file_dict) for the group.
        group_tags: list[tuple[Path, TrackTags, dict[str, str]]] = []
        for file_path in file_paths:
            ext = file_path.suffix.lower()
            try:
                match ext:
                    case ".flac":
                        file_dict = _read_tags_flac(file_path)
                    case ".mp3":
                        file_dict = _read_tags_mp3(file_path)
                    case _:  # pragma: no cover — detect_fragmented_releases only returns .flac/.mp3
                        log.warning("unify_unsupported_format", path=str(file_path), ext=ext)
                        continue
            except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
                log.warning("unify_tag_read_error", path=str(file_path), error=str(exc))
                continue
            tags = _tags_from_file_dict(file_dict)
            group_tags.append((file_path, tags, file_dict))

        if not group_tags:
            continue

        # Compute canonical destinations for every file in the group.
        # build_dest_path uses the unified path fields (cea_album_soloists_unified, etc.) that
        # are already embedded in the tags from the original annotation pipeline run.
        # global_track_idx=0 is acceptable here because CWP_MOVT_NUM is present in the tags
        # for properly annotated files (same as repath/regroup).
        stub_release = MBRelease()
        stub_track = MBTrack()

        for file_path, tags, file_dict in group_tags:
            ext = file_path.suffix.lower()
            new_dest_base = build_dest_path(dest_root, stub_release, stub_track, tags, global_track_idx=0)
            new_dest = new_dest_base.with_suffix(ext)

            if new_dest == file_path:
                log.debug("unify_noop", path=str(file_path.relative_to(dest_root)))
                continue

            acoustid = file_dict.get("ACOUSTID_ID", "")
            length_str = file_dict.get("LENGTH", "0")
            try:
                length_ms = int(length_str) if length_str else 0
            except ValueError:
                length_ms = 0

            plan_pairs.append((file_path, new_dest, acoustid, length_ms, release_id))
            log.info(
                "unify_plan",
                old=str(file_path.relative_to(dest_root)),
                new=str(new_dest.relative_to(dest_root)),
                release_id=release_id,
                dry_run=dry_run,
            )

    if not plan_pairs:
        log.info("unify_nothing_to_unify", dest_root=str(dest_root))
        return

    # --- Collision detection and resolution ---
    collision_pairs = [(src, dest, acust, length) for src, dest, acust, length, _ in plan_pairs]
    collision_results = _assess_collisions(collision_pairs)
    confirmed_nonmatches = [r for r in collision_results if r.match is False]
    if confirmed_nonmatches:
        stub_plan = [CopyPlanEntry(idx=0, src_file=src, dest_file=dest) for src, dest, _, _, _ in plan_pairs]
        stub_release_for_suffix = MBRelease()
        _apply_collision_suffix(stub_plan, confirmed_nonmatches, stub_release_for_suffix, dest_root)
        plan_pairs = [
            (entry.src_file, entry.dest_file, acust, length, rid)
            for entry, (_, _, acust, length, rid) in zip(stub_plan, plan_pairs)
        ]
        log.warning("unify_collision_suffix_applied", count=len(confirmed_nonmatches))

    if dry_run:
        for current_path, new_dest, _, _, release_id in plan_pairs:
            log.info(
                "unify_dry_run",
                old=str(current_path.relative_to(dest_root)),
                new=str(new_dest.relative_to(dest_root)),
                release_id=release_id,
            )
        return

    # --- Confirmation prompt ---
    if not yes:
        _console.print("\n[bold yellow]unify[/] will move the following files:\n")
        for current_path, new_dest, _, _, release_id in plan_pairs:
            _console.print(
                f"  [dim]{_markup_escape(str(current_path.relative_to(dest_root)))}[/]\n"
                f"    → [green]{_markup_escape(str(new_dest.relative_to(dest_root)))}[/]"
                f"  [dim](release {_markup_escape(release_id)})[/]"
            )
        _console.print(f"\n[bold]{len(plan_pairs)} file(s) will be moved.[/]  Proceed? [dim](y/n)[/]")
        _console.print("\n[bold cyan]>[/] ", end="")
        answer = input("").strip().lower()
        if answer not in {"y", "yes"}:
            log.info("unify_aborted", dest_root=str(dest_root))
            return

    # --- Perform moves, verify, journal ---
    now = datetime.datetime.now(datetime.UTC).isoformat()
    for current_path, new_dest, _, _, release_id in plan_pairs:
        # a. Capture source SHA-256 and mtime before the move
        src_hash = _sha256_file(current_path)
        src_stat = current_path.stat()
        src_mtime = src_stat.st_mtime

        # b. Ensure parent directory exists; move atomically
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(current_path, new_dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # Cross-filesystem fallback: copy + verify + unlink
            shutil.copy2(current_path, new_dest)
            # Verify the copy before unlinking the source
            cross_hash = _sha256_file(new_dest)
            if cross_hash != src_hash:
                new_dest.unlink(missing_ok=True)
                raise RuntimeError(
                    f"cross-fs copy integrity failure for '{current_path.name}': "
                    f"src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {cross_hash[:12]}…"
                ) from exc
            os.unlink(current_path)

        # c. Verify destination SHA-256 == source SHA-256
        dest_hash = _sha256_file(new_dest)
        if dest_hash != src_hash:
            raise RuntimeError(
                f"unify integrity failure for '{new_dest.name}': src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {dest_hash[:12]}…"
            )

        # d. Reconstruct tags for _verify_copy (tags are unchanged by the move)
        ext = current_path.suffix.lower()
        try:
            match ext:
                case ".flac":
                    post_dict = _read_tags_flac(new_dest)
                case ".mp3":
                    post_dict = _read_tags_mp3(new_dest)
                case _:  # pragma: no cover
                    post_dict = {}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"unify tag re-read failure for '{new_dest.name}': {exc}") from exc
        moved_tags = _tags_from_file_dict(post_dict)
        # _verify_copy checks mtime; for os.replace (same-fs rename) mtime is preserved.
        # For cross-fs copy2+unlink, shutil.copy2 copies atime/mtime so src_mtime still holds.
        _verify_copy(current_path, new_dest, moved_tags, None, src_mtime)

        # e. Journal the move with the release_id (same pattern as regroup).
        # Recording the release_id keeps the entry self-describing: future audits can re-confirm
        # without a MusicBrainz lookup, preserving P2 (journal detects, tag adjudicates).
        entry = TransactionEntry(
            timestamp=now,
            release_id=release_id,
            source=str(current_path),
            destination=str(new_dest),
            action="unified",
        )
        write_transaction_log(journal_path, [entry])
        log.info(
            "unify_moved",
            old=str(current_path.relative_to(dest_root)),
            new=str(new_dest.relative_to(dest_root)),
            release_id=release_id,
        )

        # Clean up now-empty source directories (best-effort; non-empty dirs are skipped)
        src_dir = current_path.parent
        while src_dir != dest_root:
            try:
                src_dir.rmdir()  # Only succeeds if directory is now empty
                log.info("unify_removed_empty_dir", dir=str(src_dir.relative_to(dest_root)))
                src_dir = src_dir.parent
            except OSError:
                break

    log.info("unify_complete", dest_root=str(dest_root), moved=len(plan_pairs))


def enrich(dest_root: Path, *, re_resolve: bool = False, dry_run: bool = False, acoustid_key: str = "") -> None:
    """Retroactively backfill fingerprint fields (``audio_hash``, ``chromaprint_fp``, ``acoustid_id``) into library files.

    Reads the transaction journal at ``dest_root``, resolves the current on-disk path for each
    library file (following the ``"tagged"`` → ``"repathed"`` → ``"regrouped"`` → ``"enriched"``
    lineage), and for each FLAC or MP3 file calls
    :func:`~music_annotator._pipeline_io._needs_enrich` to determine which fields are missing.

    This is an idempotent, re-runnable maintenance mode (P-FP3): a second run on a fully-enriched
    library is a no-op.  The provenance-chain invariant (P-FP4) is preserved: a journal entry with
    ``action="enriched"`` is appended **only** after
    :func:`~music_annotator._pipeline_io._verify_copy` confirms the tag round-trip.

    **Anchor rule (P-FP1):** ``audio_hash`` is never overwritten, even under ``re_resolve=True``.

    When ``re_resolve=True`` and ``acoustid_key`` is non-empty, calls
    :func:`~music_annotator._mb_api._fetch_acoustid_lookup_raw` after recomputing ``chromaprint_fp``
    to obtain the top AcoustID cluster UUID and backfill ``acoustid_id``.  When the lookup returns
    no results, ``acoustid_id`` is left unchanged (inconclusive).

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param re_resolve: When ``True``, recompute ``chromaprint_fp`` even when already present in
        the file's tags.  ``audio_hash`` is never recomputed regardless of this flag.
    :param dry_run: When ``True``, log planned backfills without writing any tags or journal
        entries.
    :param acoustid_key: AcoustID application API key.  When set together with ``re_resolve=True``,
        performs a keyed fingerprint lookup after recomputing ``chromaprint_fp`` and backfills
        ``acoustid_id`` with the top AcoustID cluster UUID.  Has no effect when ``re_resolve`` is
        ``False``.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = read_journal(journal_path)

    # --- Resolve current on-disk path for each logical library file ---
    # Walk entries in chronological order.  "tagged" seeds the map; "repathed", "regrouped",
    # and "enriched" update it.  "enriched" has source == destination so it does not change
    # the path, but we process it for completeness.
    current_lib: dict[Path, str] = {}  # current_path -> release_id (from the latest tagged entry)

    for entry in journal.entries:
        dest_path = Path(entry.destination)
        if entry.action == "tagged":
            current_lib[dest_path] = entry.release_id
        elif entry.action in {"repathed", "regrouped"}:
            old_path = Path(entry.source)
            current_lib.pop(old_path, None)
            release_id_for_path = current_lib.get(old_path, entry.release_id)
            current_lib[dest_path] = release_id_for_path
        elif entry.action == "enriched":
            # In-place update: source == destination, path unchanged.
            # Re-register to keep release_id current.
            current_lib[dest_path] = entry.release_id

    # Filter to files that actually exist on disk and are FLAC or MP3
    existing_files: list[tuple[Path, str]] = [
        (p, rid) for p, rid in current_lib.items() if p.exists() and p.suffix.lower() in {".flac", ".mp3"}
    ]

    if not existing_files:
        log.info("enrich_nothing_to_enrich", dest_root=str(dest_root))
        return

    # --- Per-file enrichment ---
    now = datetime.datetime.now(datetime.UTC).isoformat()
    count_enriched = 0
    count_noop = 0
    count_dry_run = 0
    count_inconclusive_acoustid = 0

    for current_path, release_id in existing_files:
        fields = _needs_enrich(current_path, re_resolve)

        # Determine which fields actually need a tag write (acoustid_id is copy-only, not a write)
        write_fields = {k: v for k, v in fields.items() if k in {"audio_hash", "chromaprint_fp"}}

        # When re-resolving with an AcoustID key, perform a keyed fingerprint lookup to backfill
        # acoustid_id.  This rides the same re-tag → _verify_copy → journal provenance chain as
        # audio_hash and chromaprint_fp.  Only attempted when chromaprint_fp was (re)computed
        # (i.e. it is present in write_fields), so that the lookup uses a fresh fingerprint.
        # When the lookup returns no results, acoustid_id is left unchanged (inconclusive).
        if re_resolve and acoustid_key and "chromaprint_fp" in write_fields:
            _enrich_fp = write_fields["chromaprint_fp"]
            _enrich_dur_s = _read_duration_ms(current_path) // 1000
            _, _enrich_top_uuid = _fetch_acoustid_lookup_raw(_enrich_fp, _enrich_dur_s, acoustid_key)
            if _enrich_top_uuid:
                write_fields["acoustid_id"] = _enrich_top_uuid

        if not write_fields:
            # No tag writes needed — file is already fully enriched (or fpcalc unavailable)
            log.debug("enrich_noop", path=str(current_path.relative_to(dest_root)))
            count_noop += 1
            continue

        # Count inconclusive acoustid for files that need enrichment but have no acoustid tag
        if "acoustid_id" not in fields:
            count_inconclusive_acoustid += 1

        if dry_run:
            log.info(
                "enrich_dry_run",
                path=str(current_path.relative_to(dest_root)),
                fields=list(write_fields.keys()),
            )
            count_dry_run += 1
            continue

        # --- Read current tags, update enriched fields, write back ---
        ext = current_path.suffix.lower()
        try:
            match ext:
                case ".flac":
                    file_dict = _read_tags_flac(current_path)
                case ".mp3":
                    file_dict = _read_tags_mp3(current_path)
                case _:  # pragma: no cover — filtered to .flac/.mp3 above
                    log.warning("enrich_unsupported_format", path=str(current_path), ext=ext)
                    continue
        except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
            log.warning("enrich_tag_read_error", path=str(current_path), error=str(exc))
            continue

        # Merge enriched fields into the tag dict and reconstruct a TrackTags for _verify_copy
        for field_key, field_value in write_fields.items():
            file_dict[field_key.upper()] = field_value
        tags = _tags_from_file_dict(file_dict)

        try:
            match ext:
                case ".flac":
                    apply_tags_flac(current_path, tags)
                case ".mp3":
                    apply_tags_mp3(current_path, tags)
                case _:  # pragma: no cover
                    pass
        except MutagenError as exc:
            raise RuntimeError(f"enrich tag write failure for '{current_path.name}': {exc}") from exc

        # Capture mtime after write for _verify_copy (no os.utime restore for in-place enrichment)
        post_mtime = current_path.stat().st_mtime
        _verify_copy(current_path, current_path, tags, None, post_mtime)

        # Build the full triple for the journal entry; prefer newly-computed values, fall back to
        # what was already in the file dict before the write.
        final_audio_hash = fields.get("audio_hash", "") or file_dict.get("AUDIO_HASH", "")
        final_chromaprint_fp = fields.get("chromaprint_fp", "") or file_dict.get("CHROMAPRINT_FP", "")
        final_acoustid_id = fields.get("acoustid_id", "") or file_dict.get("ACOUSTID_ID", "")

        entry = TransactionEntry(
            timestamp=now,
            release_id=release_id,
            source=str(current_path),
            destination=str(current_path),
            action="enriched",
            audio_hash=final_audio_hash,
            chromaprint_fp=final_chromaprint_fp,
            acoustid_id=final_acoustid_id,
        )
        write_transaction_log(journal_path, [entry])
        log.info(
            "enrich_written",
            path=str(current_path.relative_to(dest_root)),
            fields=list(write_fields.keys()),
        )
        count_enriched += 1

    log.info(
        "enrich_complete",
        dest_root=str(dest_root),
        enriched=count_enriched,
        noop=count_noop,
        dry_run=count_dry_run,
        inconclusive_acoustid=count_inconclusive_acoustid,
    )


# Re-export for __init__.py convenience
__all__ = [
    "CollisionPolicy",
    "run",
    "repath",
    "regroup",
    "enrich",
]
