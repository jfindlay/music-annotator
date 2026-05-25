"""Top-level annotation pipeline for music-annotator.

Provides :func:`run`, the main entry point that copies and tags a classical music album using
MusicBrainz metadata.  Also provides :class:`CollisionPolicy`, :func:`_select_medium`,
:func:`_prompt_collision_policy`, and :func:`_dedup_plan_entries` as extracted helpers.
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
    _get_bottom_work,
    fetch_acoustid_id,
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
    _sha256_file,
    _verify_copy,
    find_source_files,
    parse_disc_title,
    parse_disc_toc,
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


def _dedup_plan_entries(
    plan: list[CopyPlanEntry],
) -> list[CopyPlanEntry]:
    """Resolve duplicate destination paths caused by multiple tracks sharing the same MB ordering-key.

    When several recordings are all partial performances of the same bottom-level MB work they carry
    identical ``CWP_ORDERING_KEY_0`` values (e.g. ``3``), which causes ``build_dest_path`` to produce
    the same leaf filename for every one of them.  This function detects such duplicates *after* the
    initial plan is built and re-numbers the conflicting entries using a compound prefix of the form
    ``{ordering_key}.{global_idx:02d}`` (e.g. ``03.10``, ``03.11``, …, ``03.16``), where
    ``global_idx`` is ``CopyPlanEntry.idx + 1`` — the 1-based global running index of the source
    file across all media in the session.  Using the global index (rather than the per-disc
    ``MBTrack.position``) guarantees uniqueness even when the work spans multiple discs.

    Plan entries whose destination file is already unique are not modified.

    :param plan: The list of :class:`~music_annotator.models.CopyPlanEntry` items produced by ``run()``.
    :returns: A new :class:`~music_annotator.models.CopyPlanEntry` list with duplicate destinations
        renamed.  Non-duplicate entries are returned unchanged (same objects).
    """
    # Group plan indices by destination path
    by_dest: dict[Path, list[int]] = defaultdict(list)
    for plan_i, entry in enumerate(plan):
        by_dest[entry.dest_file].append(plan_i)

    result = list(plan)
    for dest, plan_indices in by_dest.items():
        if len(plan_indices) <= 1:
            continue
        # Extract the nn prefix from the stem (e.g. "03" from "03 - Title")
        stem = dest.stem
        nn, _, rest = stem.partition(" - ")
        suffix = dest.suffix
        parent = dest.parent
        # Re-number each duplicate in global index order so file ordering is preserved
        for plan_i in sorted(plan_indices, key=lambda i: plan[i].idx):
            global_idx = plan[plan_i].idx + 1  # 1-based global running index
            new_stem = f"{nn}.{global_idx:02d} - {rest}"
            new_dest = parent / f"{new_stem}{suffix}"
            result[plan_i] = CopyPlanEntry(idx=plan[plan_i].idx, src_file=plan[plan_i].src_file, dest_file=new_dest)
            log.info("dedup_rename", original=dest.name, renamed=new_dest.name, global_idx=global_idx)

    return result


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
       If any destination files already exist (and ``dry_run`` is ``False``), the ``collision_policy``
       determines behaviour:

       * :attr:`CollisionPolicy.ASK` — prompt interactively (default).
       * :attr:`CollisionPolicy.OVERWRITE` — replace all conflicting files.
       * :attr:`CollisionPolicy.SKIP` — copy only new files, leave existing files untouched.
       * :attr:`CollisionPolicy.ABORT` — raise :exc:`SystemExit` without copying anything.

    6. Copy each source file to the destination tree, apply tags, and restore source timestamps.
    7. Append a :class:`~music_annotator.models.TransactionEntry` per file to
       ``<dest_root>/music_annotator_journal.json`` (created or updated atomically).

    :param release_id: The MusicBrainz release MBID.
    :param src_dir: Directory containing the source audio files.  Files are matched to release tracks by sorted filename
        order.  For multi-medium releases the correct disc is identified first by matching the CD frame offsets from
        ``00 - disc info.yaml`` (if present) against each medium's disc TOC data, then by track count, then by FreeDB
        title token matching, then by a disc-number suffix in the directory name.  When the title-match heuristic is
        used, ``ui.confirm_disc`` is called to prompt the user unless ``dry_run`` is ``True``.  A :exc:`ValueError` is
        raised when no medium can be matched.
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
    :raises mb.ResponseError: On a non-retryable MusicBrainz API error.
    :raises RuntimeError: If all retry attempts are exhausted for any API call, or if post-copy verification fails (copy
        integrity, tag round-trip, cover art, or mtime mismatch).
    :raises ValueError: If no medium in the release matches the source file count for a multi-medium release.
    :raises OSError: If source files cannot be read or destination files cannot be written.
    :raises SystemExit: With code 1 if the collision policy is ABORT (or the user chooses abort interactively), if the
        user aborts the disc-selection confirmation prompt, or if the user aborts a name-shortening prompt.
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
    all_track_pairs: list[tuple[MBTrack, int]] = [(t, medium_pos) for t in selected_medium.track_list]
    log.info("release_tracks", count=len(all_track_pairs), disc=medium_pos)

    if len(src_files) != len(all_track_pairs):
        raise RuntimeError(
            f"track count mismatch for release '{release.title}': "
            f"{len(src_files)} source file(s) but {len(all_track_pairs)} track(s) on disc {medium_pos}"
        )

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

    # Pair each source file with its (MBTrack, medium_pos)
    file_track_pairs = list(zip(src_files, all_track_pairs))

    # tags_map: index → TrackTags
    tags_map: dict[int, TrackTags] = {}

    if fetch_rels and not dry_run:
        log.info("fetch_recording_rels_start")
        for idx, (src_file, (track, _medium_pos)) in enumerate(file_track_pairs):
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

            tags_map[idx] = build_track_tags(release, track, _medium_pos, rec_detail, work_hierarchy)
            tags_map[idx].acoustid_id = fetch_acoustid_id(rec_id)

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

            # Unify cwp_composer_lastnames / cwp_composers across all movements of this
            # work on this medium.  When MB credits a completion or arranger as "composer"
            # with the "additional" attribute on only some movements, those movements have
            # an empty role_buckets.composers and fall back to additional_composers.
            # build_track_tags marks this case with cwp_composers_is_fallback="1".  The
            # result is a different CWP_COMPOSER_LASTNAMES — and therefore a different
            # top_dir — than the movements that carry a plain primary-composer relation.
            # Fix: propagate the primary-composer values from any movement that has them
            # (cwp_composers_is_fallback empty) to all movements that used the fallback,
            # so every movement in the group lands in the same top-level directory.
            #
            # Note: this spans movements on the same medium only.  Cross-medium
            # unification is not supported (music-annotator operates on a
            # single-medium workspace).
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
            # this work on this medium.  All tracks in the group use this unified value for
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
            # Note: this spans movements on the same medium only.  Cross-medium unification is
            # not supported (music-annotator operates on a single-medium workspace).
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

    else:
        label_info = release.label_info_list[0] if release.label_info_list else None
        for idx, (_src_file, (track, _medium_pos)) in enumerate(file_track_pairs):
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

    # Build the full copy plan before touching the filesystem.
    plan: list[CopyPlanEntry] = []
    for idx, (src_file, (track, _medium_pos)) in enumerate(file_track_pairs):
        final_tags = tags_map[idx]
        # Pass 1-based global track index so build_dest_path can use it as the leaf nn
        # fallback when CWP_ORDERING_KEY_0 is absent.  This guarantees globally unique,
        # monotonically increasing filenames for multi-disc works with sparse MB ordering-key data.
        dest_base = build_dest_path(dest_root, release, track, final_tags, global_track_idx=idx + 1)
        dest_file = dest_base.with_suffix(src_file.suffix.lower())
        log.info("copy_track", src=src_file.name, dest=str(dest_file.relative_to(dest_root)))
        plan.append(CopyPlanEntry(idx=idx, src_file=src_file, dest_file=dest_file))

    # Resolve duplicate destination paths that arise when multiple tracks share the same
    # non-zero CWP_ORDERING_KEY_0.  The dedup pass appends ".{global_idx:02d}" to the
    # ordering-key prefix so each file gets a unique name using the global 1-based running
    # index across all source files (e.g. "03.10 - Title.flac", "03.11 - Title.flac", …).
    plan = _dedup_plan_entries(plan)

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


# Re-export for __init__.py convenience
__all__ = [
    "CollisionPolicy",
    "run",
]
