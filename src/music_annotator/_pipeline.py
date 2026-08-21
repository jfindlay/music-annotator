"""Top-level annotation pipeline for music-annotator.

Provides :func:`run`, the main entry point that copies and tags a classical music album using
MusicBrainz metadata.  Also provides :class:`CollisionPolicy`, :func:`_select_medium`,
:func:`_prompt_collision_policy`, :func:`_prompt_duration_warnings`, :func:`_apply_collision_suffix`,
:func:`_collision_suffix`, :func:`_apply_workgroup_unification`, and
:func:`_copy_tag_verify_journal_pass` as extracted helpers.

Maintenance-mode commands (:func:`~music_annotator._pipeline_maint.repath`,
:func:`~music_annotator._pipeline_maint.regroup`,
:func:`~music_annotator._pipeline_maint.unify`,
:func:`~music_annotator._pipeline_maint.enrich`) and their shared primitives live in
:mod:`music_annotator._pipeline_maint`.
"""

from __future__ import annotations

import datetime
import enum
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
    fetch_cover_art,
    fetch_recording_detail,
    fetch_release,
    init_mb,
)
from music_annotator._pipeline_io import (
    _DISC_INFO_FILENAME,
    JOURNAL_FILENAME,
    PROVENANCE_FILENAME,
    AudioCompareResult,
    _assess_collisions,
    _audio_hash,
    _find_freedb_sidecar,
    _isrc_matches,
    _read_duration_ms,
    _read_recording_id_tag,
    _run_fpcalc,
    _sha256_file,
    _verify_copy,
    _write_provenance_fields,
    check_duration_preflight,
    find_source_files,
    parse_disc_title,
    parse_disc_toc,
    write_transaction_log,
)
from music_annotator._tagger import apply_tags_flac, apply_tags_mp3
from music_annotator._tags import (
    _CLASS_VOCAB,
    _NAME_MAX,
    _proposed_short,
    _work_top_dir,
    build_dest_path,
    build_track_tags,
    collect_applied_case_ids,
)
from music_annotator._works import build_work_hierarchy, select_primary_performance_work, work_group_modal_depth
from music_annotator.models import (
    AccurateRipSummary,
    AccurateRipTrack,
    CensusSignal,
    CopyPlanEntry,
    CoverArt,
    CoverImage,
    MBMedium,
    MBRelease,
    MBTrack,
    MBWork,
    ProvenanceSidecar,
    TrackTags,
    TransactionEntry,
    classify_annotation_tier,
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
    """Minimal UI protocol for disc-selection, name-shortening, and count-mismatch confirmation in :func:`run`.

    A structural subset of :class:`~music_annotator._discover.DiscoverUI` — any object that
    implements :meth:`confirm_disc`, :meth:`confirm_shortened_name`, and
    :meth:`confirm_count_mismatch` satisfies this protocol.
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

    def confirm_count_mismatch(
        self,
        src_dir: Path,
        release: MBRelease,
        selected_medium: MBMedium | None,
        n_src: int,
        n_medium: int,
        diagnostic: str,
    ) -> bool:
        """Prompt the operator to accept or decline a track-count mismatch.

        Called when the number of source files does not match the selected medium's track count,
        or when no medium in a multi-disc release matches the source file count.

        :param src_dir: The source directory being processed.
        :param release: The MusicBrainz release being ingested.
        :param selected_medium: The best-candidate medium for ingest, or ``None`` when no medium
            matched the source file count.
        :param n_src: Number of source audio files in ``src_dir``.
        :param n_medium: Number of tracks on ``selected_medium`` (0 when ``selected_medium`` is ``None``).
        :param diagnostic: Human-readable edition-vs-structure context string (display only).
        :returns: ``True`` to accept (ingest at ``mb-partial``), ``False`` to decline (skip).
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
    work_dirs = sorted({_work_top_dir(p, dest_root).relative_to(dest_root) for p in collisions})
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

    A collision suffix cannot be derived without a release id: an empty id would produce an
    empty suffix, silently corrupting the library layout instead of disambiguating it.  Any
    caller that passes a release with an empty id has a threading defect; fail loud so the
    defect is caught immediately rather than silently degrading the library.

    :param release: The :class:`~music_annotator.models.MBRelease` being processed.
    :returns: A non-empty suffix string suitable for appending as ``[<suffix>]``.
    :raises ValueError: If no non-empty suffix can be derived (release id is empty and no
        catalog number is present), indicating a caller threading defect.
    """
    if release.label_info_list:
        cat = release.label_info_list[0].catalog_number.strip()
        if cat:
            return cat
    suffix = release.id[:8]
    if not suffix:
        raise ValueError(
            "a collision suffix cannot be derived without a release id: "
            "release.id is empty, which would produce an empty '[]' suffix and silently "
            "corrupt the library layout; ensure the release id is threaded through to the "
            "collision suffix builder"
        )
    return suffix


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
        # Rewrite the work_dir component to add the release suffix.
        # Destination structure (class-prefixed): dest_root / class / top_dir / work_dir / … leaf
        # Destination structure (legacy):         dest_root / top_dir / work_dir / … leaf
        # Discriminate by testing whether parts[0] is a known class name (C-CLASS vocabulary).
        rel_parts = list(entry.dest_file.relative_to(dest_root).parts)
        work_dir_idx = 2 if rel_parts[0] in _CLASS_VOCAB else 1
        rel_parts[work_dir_idx] = f"{rel_parts[work_dir_idx]} [{suffix}]"
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
    work_top_dir.mkdir(parents=True, exist_ok=True)
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


def _write_whipper_sidecars(
    src_dir: Path,
    work_top_dir: Path,
    whipper_sidecars_written: set[Path],
    journal_entries: list[TransactionEntry],
    now: str,
    release_id: str,
) -> None:
    """Copy whipper sidecar files (``.log``, ``.cue``, ``.toc``) from ``src_dir`` to ``work_top_dir``.

    Preserves whipper rip provenance files losslessly in the destination library.  Each file is
    copied with a SHA-256 integrity check; a mismatch raises :exc:`RuntimeError`.  A ``"sidecar"``
    journal entry is appended for each file successfully copied.

    A set ``whipper_sidecars_written`` keyed on ``work_top_dir`` ensures the copy is performed at
    most once per work directory across all tracks in the copy loop.

    The ``"sidecar"`` journal entries produced here are **not** ``"tagged"`` entries and must not
    feed the "safe to delete source" message (C-MOVE provenance invariant).

    :param src_dir: Source directory containing the whipper rip files.
    :param work_top_dir: Work top directory (``dest_root / composer-dir / work-dir``) where the
        sidecar files are written.
    :param whipper_sidecars_written: Mutable set of work_top_dirs already processed this run.
    :param journal_entries: Mutable list to which new ``"sidecar"`` entries are appended.
    :param now: ISO-format timestamp string for journal entries.
    :param release_id: MusicBrainz release MBID for journal entries.
    :raises RuntimeError: When the SHA-256 of a written file does not match the source.
    """
    if work_top_dir in whipper_sidecars_written:
        return
    whipper_sidecars_written.add(work_top_dir)

    _whipper_sidecar_extensions: frozenset[str] = frozenset({".log", ".cue", ".toc"})
    for src_file in sorted(src_dir.iterdir()):
        if src_file.suffix.lower() not in _whipper_sidecar_extensions:
            continue
        if not src_file.is_file():
            continue
        dest_file = work_top_dir / src_file.name
        src_data = src_file.read_bytes()
        src_hash = hashlib.sha256(src_data).hexdigest()
        work_top_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        dest_hash = hashlib.sha256(dest_file.read_bytes()).hexdigest()
        if dest_hash != src_hash:
            raise RuntimeError(
                f"whipper sidecar copy integrity failure for '{dest_file.name}': "
                f"src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {dest_hash[:12]}…"
            )
        log.debug("whipper_sidecar_written", dest=str(dest_file))
        journal_entries.append(
            TransactionEntry(
                timestamp=now,
                release_id=release_id,
                source=str(src_file),
                destination=str(dest_file),
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
        # The audio suffix for the leaf is taken from the source file, not from Path.suffix on the
        # leaf itself.  Path.suffix would misidentify trailing dots in work titles (e.g. "op." in
        # "01 - Sonata op. 23.flac") as the extension.  The source suffix is always the correct one.
        leaf_audio_suffix = entry.src_file.suffix.lower()
        leaf_part = rel_parts[-1]
        for part in rel_parts:
            if len(part.encode("utf-8")) > _NAME_MAX and part not in subs:
                # For leaf parts, reserve the audio suffix's bytes so that stem+suffix ≤ _NAME_MAX.
                # For intermediate directory components, no suffix reservation is needed.
                part_audio_suffix = leaf_audio_suffix if part == leaf_part else ""
                proposed = _proposed_short(part, part_audio_suffix)
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


def _apply_workgroup_unification(
    tags_map: dict[int, TrackTags],
    release: MBRelease,
    top_work_groups: dict[str, list[int]],
) -> None:
    """Apply all cross-movement unification passes to ``tags_map`` for every top-work group.

    Iterates over ``top_work_groups`` and applies five sequential passes to the tracks in each
    group:

    1. **Movement numbers**: assigns ``movementnumber`` / ``movementtotal`` / ``cwp_movt_num`` /
       ``cwp_movt_tot`` / ``cwp_single_work_album`` based on position within the group.
    2. **Intermediate sibling index** (C-L1): for each intermediate hierarchy level ``i >= 1``,
       ranks distinct sibling nodes by ascending ``cwp_ordering_key_{i}`` and writes a gap-free
       1-based ``cwp_inter_index_{i}`` to every track belonging to each node.
    3. **Composer unification**: propagates primary-composer values from any movement that has a
       plain (non-additional) composer relation to all movements that fell back to
       ``additional_composers`` (``cwp_composers_is_fallback="1"``).
    4. **Recording-date unification**: computes the minimum-begin / maximum-end span across all
       movements and writes it to ``recording_date_work`` so every movement lands in the same
       destination directory.
    5. **First-release-date normalisation**: when no session date exists in the group (``_begins``
       is empty), normalises every movement's ``recording_first_release_date`` to the release year
       so the ``[rel YYYY]`` fallback is uniform.

    Mutates ``tags_map`` in-place.  Does not return a value.

    :param tags_map: Global-index → :class:`~music_annotator.models.TrackTags` mapping over all
        media of the release.  Mutated in-place.
    :param release: The :class:`~music_annotator.models.MBRelease` being processed; used to derive
        the normalising release year for the first-release-date pass.
    :param top_work_groups: Mapping from top-work MBID to the list of global indices (into
        ``tags_map``) that belong to that work group.
    """
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
                if len(release.release_group.first_release_date) >= 4 and release.release_group.first_release_date[:4].isdigit()
                else ""
            )
            if _rel_year:
                for grp_idx in group_idxs:
                    if tags_map[grp_idx].recording_first_release_date:
                        tags_map[grp_idx].recording_first_release_date = _rel_year


def _copy_tag_verify_journal_pass(
    plan: list[CopyPlanEntry],
    tags_map: dict[int, TrackTags],
    cover: CoverArt,
    src_dir: Path,
    dest_root: Path,
    release_id: str,
    medium_pos: int,
    skip_dest: set[Path],
    dry_run: bool,
    acoustid_key: str,
    census_signal: CensusSignal = CensusSignal.SEARCH_HIT,
    ar_summary: AccurateRipSummary | None = None,
    ar_tracks: dict[int, AccurateRipTrack] | None = None,
) -> list[TransactionEntry]:
    """Execute the copy / tag / verify / journal loop for the selected medium's tracks.

    This is the C-PROV chain.  For each entry in ``plan`` the ordering is strictly:

    1. Capture source SHA-256 and timestamps.
    2. ``shutil.copy2`` the file to the destination.
    3. Verify destination SHA-256 equals source SHA-256 — raise :exc:`RuntimeError` on mismatch.
    4. Compute ``audio_hash`` and ``acoustid_fingerprint`` from the source.
    5. Optionally confirm the AcoustID identity (read-only diagnostic; never raises).
    6. Set cover-art sidecar reference tags on ``final_tags``.
    7. Apply AccurateRip flat fields to ``final_tags`` from ``ar_tracks`` (when provided).
    8. Apply tags via :func:`~music_annotator._tagger.apply_tags_flac` or
       :func:`~music_annotator._tagger.apply_tags_mp3`.
    9. Restore source timestamps via ``os.utime``.
    10. Verify the copy via :func:`~music_annotator._pipeline_io._verify_copy` — raise
        :exc:`RuntimeError` on any mismatch.
    11. Accumulate applied contested-default case-IDs for this track into the per-work-dir set.
    12. Write the annotation tier, AccurateRip summary, and applied case-IDs to the provenance
        sidecar.  The annotation tier and AccurateRip summary are written once per work directory;
        applied case-IDs are written on every track using the set-union merge so all tracks'
        contributions are captured.
    13. Write sidecar cover-art files, the FreeDB YAML, and whipper sidecars (once per work dir).
    14. Append a ``"tagged"`` :class:`~music_annotator.models.TransactionEntry` to the result list.

    Dry-run and skip-dest entries are handled before step 1 and produce ``"dry_run"`` or
    ``"skipped"`` journal entries respectively.

    :param plan: The list of :class:`~music_annotator.models.CopyPlanEntry` objects for the
        selected medium.
    :param tags_map: Global-index → :class:`~music_annotator.models.TrackTags` mapping over all
        media of the release.
    :param cover: The :class:`~music_annotator.models.CoverArt` instance for this release.
    :param src_dir: Source directory; used to locate the FreeDB disc-info YAML sidecar.
    :param dest_root: Root directory of the destination library; used to derive the work top
        directory for sidecar writes.
    :param release_id: MusicBrainz release MBID; written into every journal entry.
    :param medium_pos: 1-based disc position of the selected medium; used to name the FreeDB YAML
        sidecar (``freedb_disc_{medium_pos}.yaml``).
    :param skip_dest: Set of destination paths that should be skipped (collision-skip policy).
    :param dry_run: When ``True``, log planned operations without copying or writing any files.
    :param acoustid_key: AcoustID application API key.  When set and a fingerprint is available,
        performs a keyed lookup and logs whether the selected recording MBID is confirmed or
        contradicted.  Never alters the copy/tag/verify path.
    :param census_signal: The identity evidence signal for this ingest, used to derive the
        annotation tier written to the provenance sidecar.  Defaults to
        :attr:`~music_annotator.models.CensusSignal.SEARCH_HIT` (``mb-search-resolved``).
    :param ar_summary: Optional per-release AccurateRip summary (C-AR) from
        :func:`~music_annotator._pipeline_io.parse_whipper_log`.  When provided, written to the
        provenance sidecar under the monotonic-upgrade rule.
    :param ar_tracks: Optional per-track AccurateRip data (C-AR) keyed by 1-based track position.
        When provided, the 11 flat fields are projected onto each ``TransactionEntry`` and the
        corresponding ``TrackTags`` before tagging.
    :returns: List of :class:`~music_annotator.models.TransactionEntry` objects produced during
        this pass (one per plan entry, plus sidecar entries).
    :raises RuntimeError: If copy integrity fails, tag write fails, or ``_verify_copy`` fails.
    :raises OSError: If source files cannot be read or destination files cannot be written.
    """
    journal_entries: list[TransactionEntry] = []
    sidecars_written: set[Path] = set()
    freedb_written: set[Path] = set()
    whipper_sidecars_written: set[Path] = set()
    tier_written: set[Path] = set()
    # Accumulates the union of applied contested-default case-IDs across all tracks in each work
    # directory.  Keyed on work_top_dir, parallel to tier_written.  Written to the provenance
    # sidecar at the same gated site as annotation_tier (after _verify_copy, before journal append).
    case_ids_accumulated: dict[Path, set[str]] = {}
    now = datetime.datetime.now(datetime.UTC).isoformat()
    annotation_tier, needs_spot_check = classify_annotation_tier(census_signal)
    _ar_tracks: dict[int, AccurateRipTrack] = ar_tracks if ar_tracks is not None else {}
    _ar_summary: AccurateRipSummary = ar_summary if ar_summary is not None else AccurateRipSummary()

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
        final_tags.acoustid_fingerprint = _run_fpcalc(src_file)

        # AcoustID identity-confirm: when an API key is supplied and a fingerprint is available,
        # look up the recording MBIDs for this file and check whether the selected recording MBID
        # is among them.  The cluster UUID from this lookup is the AcoustID cluster UUID — Picard's
        # acoustid_id source — and is written to final_tags.acoustid_id.  When no api_key is
        # supplied or fpcalc yields no fingerprint, acoustid_id is left empty (empty-not-fallback
        # rule: never re-filled from list_by_mbid).
        if acoustid_key and final_tags.acoustid_fingerprint:
            _confirm_dur_s = _read_duration_ms(src_file) // 1000
            _confirm_mbids, _acoustid_cluster_uuid = _fetch_acoustid_lookup_raw(
                final_tags.acoustid_fingerprint, _confirm_dur_s, acoustid_key
            )
            final_tags.acoustid_id = _acoustid_cluster_uuid
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

        # Project AccurateRip flat fields onto final_tags (C-AR).  The track position is
        # the 1-based tracknumber from the MB medium; ar_tracks is keyed by that position.
        # confidence serializes as decimal string, empty when 0/absent (never "0").
        _track_pos_str = final_tags.tracknumber
        _ar_track: AccurateRipTrack | None = None
        if _track_pos_str:
            try:
                _ar_track = _ar_tracks.get(int(_track_pos_str))
            except ValueError:
                pass
        if _ar_track is not None:
            final_tags.accuraterip_v1_result = _ar_track.v1.result.value
            final_tags.accuraterip_v1_confidence = str(_ar_track.v1.confidence) if _ar_track.v1.confidence else ""
            final_tags.accuraterip_v1_local_crc = _ar_track.v1.local_crc
            final_tags.accuraterip_v1_remote_crc = _ar_track.v1.remote_crc
            final_tags.accuraterip_v2_result = _ar_track.v2.result.value
            final_tags.accuraterip_v2_confidence = str(_ar_track.v2.confidence) if _ar_track.v2.confidence else ""
            final_tags.accuraterip_v2_local_crc = _ar_track.v2.local_crc
            final_tags.accuraterip_v2_remote_crc = _ar_track.v2.remote_crc
            final_tags.accuraterip_test_crc = _ar_track.test_crc
            final_tags.accuraterip_copy_crc = _ar_track.copy_crc
            final_tags.accuraterip_status = _ar_track.status

        ext = src_file.suffix.lower()
        try:
            match ext:
                case ".flac":
                    apply_tags_flac(dest_file, final_tags, cover)
                case ".mp3":
                    apply_tags_mp3(dest_file, final_tags, cover)
                case _:  # pragma: no cover
                    log.warning("unsupported_format", ext=ext, file=dest_file.name)
        except MutagenError as exc:
            raise RuntimeError(f"tag write failure for '{dest_file.name}': {exc}") from exc

        os.utime(dest_file, src_times)

        _verify_copy(src_file, dest_file, final_tags, cover, src_stat.st_mtime)

        # Derive the work top directory and write sidecar cover art files exactly once per
        # work directory across all tracks.  _work_top_dir handles both legacy two-level paths
        # (dest_root/top_dir/work_dir/…) and class-prefixed three-level paths
        # (dest_root/class/top_dir/work_dir/…) introduced by C-CLASS.
        work_top_dir = _work_top_dir(dest_file, dest_root)

        _write_sidecars(cover, work_top_dir, sidecars_written, journal_entries, now, release_id)
        _write_freedb_yaml(src_dir, work_top_dir, medium_pos, freedb_written, journal_entries, now, release_id)
        _write_whipper_sidecars(src_dir, work_top_dir, whipper_sidecars_written, journal_entries, now, release_id)

        # Accumulate applied contested-default case-IDs for this track into the per-work-dir set.
        # Done after _verify_copy so the accumulation is gated on successful verification (C-PROV).
        _track_case_ids = collect_applied_case_ids(final_tags)
        case_ids_accumulated.setdefault(work_top_dir, set()).update(_track_case_ids)

        # C-PROV invariant: write annotation_tier, AccurateRip summary, and applied case-IDs to
        # the provenance sidecar after _verify_copy succeeds and before journal_entries.append.
        # Runs after _write_freedb_yaml so that the tier is merged into the freedb sidecar when
        # one exists, consistent with the enrich_origin_time convention.
        # AccurateRip summary monotonic-upgrade rule: an incoming empty summary must not overwrite
        # a populated one (C-AR).
        # Applied case-IDs set-union rule: the accumulated set for this work dir is passed on the
        # first track; subsequent tracks in the same work dir call _write_provenance_fields again
        # with only the new case-IDs so the set-union merge captures all tracks' contributions.
        _sidecar_path = _find_freedb_sidecar(work_top_dir)
        if _sidecar_path is None:
            _sidecar_path = work_top_dir / PROVENANCE_FILENAME
        if work_top_dir not in tier_written:
            tier_written.add(work_top_dir)
            # Apply monotonic-upgrade rule for accuraterip_summary: only write when the incoming
            # summary is populated (log_sha256 non-empty) or no existing summary is present.
            _prov_to_write = ProvenanceSidecar(
                annotation_tier=annotation_tier,
                needs_spot_check=needs_spot_check,
                applied_case_ids=sorted(case_ids_accumulated.get(work_top_dir, set())),
            )
            if _ar_summary.log_sha256:
                _prov_to_write = ProvenanceSidecar(
                    annotation_tier=annotation_tier,
                    needs_spot_check=needs_spot_check,
                    accuraterip_summary=_ar_summary,
                    applied_case_ids=sorted(case_ids_accumulated.get(work_top_dir, set())),
                )
            _write_provenance_fields(
                _sidecar_path,
                _prov_to_write,
            )
            log.debug(
                "annotation_tier_written",
                tier=str(annotation_tier),
                needs_spot_check=needs_spot_check,
                sidecar=str(_sidecar_path.relative_to(dest_root)),
            )
        elif _track_case_ids:
            # Subsequent track in the same work dir: write only the new case-IDs so the
            # set-union merge in _write_provenance_fields captures all tracks' contributions.
            _write_provenance_fields(
                _sidecar_path,
                ProvenanceSidecar(applied_case_ids=_track_case_ids),
            )

        journal_entries.append(
            TransactionEntry(
                timestamp=now,
                release_id=release_id,
                source=str(src_file),
                destination=str(dest_file),
                action="tagged",
                audio_hash=final_tags.audio_hash,
                acoustid_fingerprint=final_tags.acoustid_fingerprint,
                acoustid_id=final_tags.acoustid_id,
                accuraterip_v1_result=final_tags.accuraterip_v1_result,
                accuraterip_v1_confidence=final_tags.accuraterip_v1_confidence,
                accuraterip_v1_local_crc=final_tags.accuraterip_v1_local_crc,
                accuraterip_v1_remote_crc=final_tags.accuraterip_v1_remote_crc,
                accuraterip_v2_result=final_tags.accuraterip_v2_result,
                accuraterip_v2_confidence=final_tags.accuraterip_v2_confidence,
                accuraterip_v2_local_crc=final_tags.accuraterip_v2_local_crc,
                accuraterip_v2_remote_crc=final_tags.accuraterip_v2_remote_crc,
                accuraterip_test_crc=final_tags.accuraterip_test_crc,
                accuraterip_copy_crc=final_tags.accuraterip_copy_crc,
                accuraterip_status=final_tags.accuraterip_status,
            )
        )

    return journal_entries


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
    origin_source: str = "",
    ar_summary: AccurateRipSummary | None = None,
    ar_tracks: dict[int, AccurateRipTrack] | None = None,
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
    :param no_cache: When ``True``, bypass all on-disk metadata and image caches and always fetch from the network.
        Defaults to ``False``.
    :param disc_override: When set, bypass all automatic medium-selection heuristics and use the medium at this
        1-based disc position.  Applies to both single-medium and multi-medium releases.  The downstream track-count
        validation still runs, so a mismatch between source file count and the selected medium's track count raises
        :exc:`RuntimeError`.
    :param acoustid_key: AcoustID application API key.  When set, performs a keyed fingerprint ``/v2/lookup`` for each
        source file, captures the AcoustID cluster UUID (Picard's ``acoustid_id`` source) into the journal entry, and
        logs whether the selected recording MBID is confirmed or contradicted by the AcoustID results.  When absent or
        when fpcalc yields no fingerprint, ``acoustid_id`` is left empty (empty-not-fallback rule).
    :param origin_source: Provenance source identifier for this ingest.  When set to ``"whipper"`` and a TOC disc-ID
        match is found for a single-disc release, the annotation tier is promoted to ``full-mb-verified``
        (``CensusSignal.EMBEDDED_MBID``) rather than the conservative ``mb-search-resolved`` default.  This is the
        C-WHIP trust anchor: whipper rips carry hardware-level TOC identity, so a resolving disc-ID is equivalent to
        an embedded MBID.  A bare non-whipper single-disc TOC match keeps the conservative tier.  Defaults to ``""``.
    :param ar_summary: Optional per-release AccurateRip summary (C-AR) from
        :func:`~music_annotator._pipeline_io.parse_whipper_log`.  When provided and populated
        (``log_sha256`` non-empty), written to the provenance sidecar under the monotonic-upgrade
        rule.  Defaults to ``None`` (no AccurateRip data).
    :param ar_tracks: Optional per-track AccurateRip data (C-AR) keyed by 1-based track position.
        When provided, the 11 flat fields are projected onto each ``TrackTags`` before tagging and
        onto each ``TransactionEntry``.  Defaults to ``None`` (no per-track AccurateRip data).
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

    # accepted_mismatch: set to True when the operator accepts a track-count mismatch override.
    # Forces census_signal = CensusSignal.MISMATCH so the ingest lands at mb-partial (C-OVR).
    accepted_mismatch: bool = False

    toc_matched: bool = False  # True when the medium was selected via TOC disc-ID match.
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
            try:
                selected_medium, selection_method = _select_medium_with_reason(
                    mediums, len(src_files), src_dir.name, track_frames=track_frames, dtitle=dtitle
                )
            except ValueError as _no_match_exc:
                # Multi-disc no-match: no medium's track count equals n_src.
                # Offer the operator an override via confirm_count_mismatch when interactive.
                if ui is not None and not dry_run:
                    n_src_val = len(src_files)
                    # Pick the best medium: nearest track count to n_src; ties → lowest position.
                    best_medium = min(
                        mediums,
                        key=lambda m: (abs(len(m.track_list) - n_src_val), m.position),
                    )
                    n_medium_val = len(best_medium.track_list)
                    n_disc = len(mediums)
                    diagnostic = (
                        f"multi-disc release ({n_disc} media) — no medium matches {n_src_val} source file(s); "
                        f"nearest medium is disc {best_medium.position} with {n_medium_val} track(s)"
                    )
                    if ui.confirm_count_mismatch(src_dir, release, best_medium, n_src_val, n_medium_val, diagnostic):
                        selected_medium = best_medium
                        accepted_mismatch = True
                        k = min(n_src_val, n_medium_val)
                        src_files = src_files[:k]
                        log.info(
                            "count_mismatch_override_accepted",
                            medium_pos=best_medium.position,
                            n_src=n_src_val,
                            n_medium=n_medium_val,
                            k=k,
                        )
                    else:
                        raise _no_match_exc
                else:
                    raise _no_match_exc
            else:
                toc_matched = selection_method == SelectionMethod.TOC
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

    # Single-disc whipper TOC promotion (C-WHIP).
    # For multi-disc releases, toc_matched is set above by _select_medium_with_reason.
    # For single-disc releases that path is skipped, so toc_matched stays False even when a
    # matching 00 - disc info.yaml is present.  When the source is a whipper rip (origin_source
    # == "whipper") and the TOC resolves against the single medium's disc entries, the identity
    # evidence is hardware-level — equivalent to an embedded MBID — so promote to EMBEDDED_MBID.
    # A bare non-whipper single-disc TOC match keeps the conservative tier (C-WHIP trust anchor).
    if not toc_matched and origin_source == "whipper" and track_frames is not None and len(mediums) == 1:
        toc_matched = _match_medium_by_toc([selected_medium], track_frames) is not None

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

    # Track-count mismatch gate (C-OVR).
    # When interactive (ui is not None and not dry_run), offer the operator an override that ingests
    # the positionally-aligned min(n_src, n_medium) tracks at mb-partial.  Under automation
    # (ui is None or dry_run) the original hard-fail is preserved (non-interactive contract).
    if len(src_files) != len(copy_subset) and not accepted_mismatch:
        if ui is not None and not dry_run:
            n_src_val = len(src_files)
            n_medium_val = len(copy_subset)
            n_disc = len(mediums)
            if n_disc > 1:
                diagnostic = (
                    f"single-medium count mismatch on disc {medium_pos} of {n_disc} — "
                    f"{n_src_val} source file(s) vs {n_medium_val} track(s) on this medium"
                )
            else:
                diagnostic = (
                    f"edition/pressing mismatch — {n_src_val} source file(s) vs {n_medium_val} track(s) on the MB release"
                )
            if ui.confirm_count_mismatch(src_dir, release, selected_medium, n_src_val, n_medium_val, diagnostic):
                accepted_mismatch = True
                k = min(n_src_val, n_medium_val)
                src_files = src_files[:k]
                copy_subset = copy_subset[:k]
                log.info(
                    "count_mismatch_override_accepted",
                    medium_pos=medium_pos,
                    n_src=n_src_val,
                    n_medium=n_medium_val,
                    k=k,
                )
            else:
                raise RuntimeError(
                    f"track count mismatch for release '{release.title}': "
                    f"{n_src_val} source file(s) but {n_medium_val} track(s) on disc {medium_pos}"
                )
        else:
            raise RuntimeError(
                f"track count mismatch for release '{release.title}': "
                f"{len(src_files)} source file(s) but {len(copy_subset)} track(s) on disc {medium_pos}"
            )
    elif accepted_mismatch:
        # Multi-disc no-match path already truncated src_files; truncate copy_subset to match.
        k = len(src_files)
        copy_subset = copy_subset[:k]

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
            log.info("process_recording", position=track.position, title=track.recording.title)

            rec_detail = fetch_recording_detail(rec_id, no_cache=no_cache)

            work_hierarchy: list[MBWork] = []
            # Inflate each performance-linked work stub to a full work before scoring.
            # _get_bottom_work fetches from MB only when the embedded work lacks relation data.
            performance_works = [
                _get_bottom_work(rel.work, no_cache=no_cache)
                for rel in rec_detail.work_relation_list
                if rel.type == "performance" and rel.work.id
            ]
            if performance_works:
                primary_work = select_primary_performance_work(performance_works)
                work_hierarchy = build_work_hierarchy(primary_work)

            tags_map[global_idx] = build_track_tags(release, track, _med_pos, rec_detail, work_hierarchy)

        # Compute movement numbers grouped by top work MBID.
        # Iterates the full tags_map (all media) so movements of one work that straddle a disc
        # boundary are grouped together — this is the C-S0 contract.
        top_work_groups: dict[str, list[int]] = defaultdict(list)
        for global_idx in range(len(all_media_pairs)):
            t = tags_map[global_idx]
            twid = t.cwp_workid_top or t.musicbrainz_workid
            top_work_groups[twid].append(global_idx)

        _apply_workgroup_unification(tags_map, release, top_work_groups)

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

    # Compute the work-group modal depth per group and build a per-track lookup.
    # Groups tracks by (CWP_WORKID_TOP or MUSICBRAINZ_WORKID), mirroring the scanner grouping
    # in scripts/scan_nonuniform_depth.py (which groups by (release_dir, CWP_WORKID_TOP)).
    # Within one run() call all tracks share the same release dir, so the grouping key is the
    # top-work MBID alone.  The modal depth is computed once per group and shared across all
    # tracks in that group, clamping over-resolved branches to the group ceiling per the
    # uniform-ceiling/ragged-floor rule (STYLEGUIDE 4.5 / C-W3b).
    _run_work_groups: dict[str, list[int]] = defaultdict(list)
    for _gidx in range(len(all_media_pairs)):
        _t = tags_map[_gidx]
        _twid = _t.cwp_workid_top or _t.musicbrainz_workid
        _run_work_groups[_twid].append(_gidx)

    # modal_depth_by_idx: global_idx → group modal depth (int | None).
    # None means no group context (all-orphan group, PL=0 for all tracks) — build_dest_path
    # renders own depth, which is already 0, so the result is identical.
    modal_depth_by_idx: dict[int, int | None] = {}
    for _twid, _group_idxs in _run_work_groups.items():
        _part_levels = [int(tags_map[_i].cwp_part_levels or "0") for _i in _group_idxs]
        _modal = work_group_modal_depth(_part_levels)
        # When modal is 0 (all-orphan group), pass None so build_dest_path uses own depth
        # unchanged — equivalent outcome, avoids a redundant min(0, 0) clamp.
        _modal_or_none: int | None = _modal if _modal > 0 else None
        for _i in _group_idxs:
            modal_depth_by_idx[_i] = _modal_or_none

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
        dest_base = build_dest_path(
            dest_root,
            release,
            track,
            final_tags,
            global_track_idx=copy_subset_pos + 1,
            group_modal_depth=modal_depth_by_idx.get(global_idx),
        )
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

    # --- Determine annotation tier (C-TIER) ---
    # Classify the identity evidence available for this ingest.  The signal drives the
    # annotation_tier written to the provenance sidecar inside _copy_tag_verify_journal_pass.
    #
    # Evidence hierarchy (strongest first):
    #   0. Accepted count-mismatch override (C-OVR) — operator accepted a partial ingest;
    #      forced to MISMATCH regardless of other evidence, yielding mb-partial tier.
    #   1. TOC disc-ID match — hardware-level identity, equivalent to embedded MBID.
    #      Multi-disc: always promoted (set by _select_medium_with_reason above).
    #      Single-disc: promoted only when origin_source == "whipper" (C-WHIP trust anchor);
    #      a bare non-whipper single-disc TOC match keeps the conservative tier.
    #   2. Embedded recording MBIDs in source files that match the selected medium's track list.
    #   3. Default: search-resolved (user supplied a release_id; no stronger identity evidence).
    #
    # NOTE: no-MB (source-tags-only) is not reachable from run() — it requires no release_id.
    # mb-partial is now reachable via the accepted_mismatch path (C-OVR).
    if accepted_mismatch:
        census_signal = CensusSignal.MISMATCH
    elif toc_matched:
        census_signal = CensusSignal.EMBEDDED_MBID
    else:
        selected_medium_track_ids: set[str] = {t.recording.id for t in selected_medium.track_list}
        embedded_ids = [_read_recording_id_tag(f) for f in src_files]
        has_embedded_mbid = any(eid and eid in selected_medium_track_ids for eid in embedded_ids)
        if has_embedded_mbid:
            census_signal = CensusSignal.EMBEDDED_MBID
        else:
            # ISRC-match rung (C-ISRC): promote to full-mb-verified when all present-ISRC tracks
            # match the selected medium's recording ISRC lists and at least one confirms.
            # Tracks with no source ISRC or an empty isrc_list are inconclusive (match=None) and
            # neither block promotion nor count toward confirmation.  A dir where no track yields
            # match=True stays at SEARCH_HIT (all-inconclusive rule, C-ISRC §evidence rule).
            isrc_results = [
                _isrc_matches(src_files[i], selected_medium.track_list[i].recording.isrc_list) for i in range(len(src_files))
            ]
            has_mismatch = any(r.match is False for r in isrc_results)
            has_confirmed = any(r.match is True for r in isrc_results)
            if not has_mismatch and has_confirmed:
                census_signal = CensusSignal.ISRC_MATCH
            else:
                census_signal = CensusSignal.SEARCH_HIT
    log.info("annotation_tier_signal", signal=str(census_signal))

    # --- Copy, tag, and journal ---
    journal_entries = _copy_tag_verify_journal_pass(
        plan=plan,
        tags_map=tags_map,
        cover=cover,
        src_dir=src_dir,
        dest_root=dest_root,
        release_id=release_id,
        medium_pos=medium_pos,
        skip_dest=skip_dest,
        dry_run=dry_run,
        acoustid_key=acoustid_key,
        census_signal=census_signal,
        ar_summary=ar_summary,
        ar_tracks=ar_tracks,
    )

    if not dry_run:
        write_transaction_log(dest_root / JOURNAL_FILENAME, journal_entries)

        # Count copied (not skipped/dry-run) entries and print a confirmation message so the user
        # knows it is safe to delete the source directory before they do so.
        copied = [e for e in journal_entries if e.action == "tagged"]
        dest_dirs = sorted({_work_top_dir(Path(e.destination), dest_root).relative_to(dest_root) for e in copied})
        if copied:
            _console.print(
                f"\n[bold green]Verified OK:[/] [green]{len(copied)} file(s) written and confirmed under "
                f"{_markup_escape(str(dest_root))}:[/]"
            )
            for d in dest_dirs:
                _console.print(f"  [green]{_markup_escape(str(d))}[/]")
            _console.print("[bold green]It is safe to delete the source directory.[/]\n")

    log.info("run_complete", dest=str(dest_root))
