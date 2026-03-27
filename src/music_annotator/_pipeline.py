"""Top-level annotation pipeline for music-annotator.

Provides :func:`run`, the main entry point that copies and tags a classical music album using
MusicBrainz metadata.  Also provides :class:`CollisionPolicy`, :func:`_select_medium`, and
:func:`_prompt_collision_policy` as extracted helpers.
"""

from __future__ import annotations

import datetime
import enum
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path

import structlog
from mutagen._util import MutagenError

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
    JOURNAL_FILENAME,
    _check_collisions,
    _sha256_file,
    _verify_copy,
    find_source_files,
    parse_disc_toc,
    write_transaction_log,
)
from music_annotator._tagger import apply_tags_flac, apply_tags_mp3
from music_annotator._tags import build_dest_path, build_track_tags
from music_annotator._works import build_work_hierarchy, select_primary_performance_work
from music_annotator.models import CopyPlanEntry, CoverArt, CoverImage, MBMedium, MBTrack, MBWork, TrackTags, TransactionEntry

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Disc-number suffix such as "(Disc 1)", "(disc 2)", or "Disc 1" at the end of a dir name.
_DISC_SUFFIX_RE: re.Pattern[str] = re.compile(r"\s*[\(\[]?[Dd]isc\s*\d+[\)\]]?\s*$")


class CollisionPolicy(enum.Enum):
    """Policy to apply when destination files already exist.

    Used as a parameter to :func:`run` to control behaviour when one or more planned destination
    files already exist on disk before copying begins.

    Attributes:
        ASK: Prompt the user interactively via stdin.
        SKIP: Copy only files that do not already exist; leave existing files untouched.
        OVERWRITE: Replace all existing files unconditionally.
        ABORT: Raise :exc:`SystemExit` without copying anything.
    """

    ASK = "ask"
    SKIP = "skip"
    OVERWRITE = "overwrite"
    ABORT = "abort"


def _match_medium_by_toc(mediums: list[MBMedium], track_frames: list[int]) -> MBMedium | None:
    """Return the medium whose disc TOC exactly matches ``track_frames``, or ``None``.

    Compares ``track_frames`` (per-track CD frame offsets from the source directory's
    ``00 - disc info.yaml``) against the ``offsets`` list on every :class:`~music_annotator.models.MBDisc`
    entry attached to each medium.  A medium may carry more than one disc entry (different pressings),
    so all are checked.  Returns ``None`` when no medium has a matching disc, or when the release was
    fetched without ``discids`` includes (i.e. all ``disc_list`` fields are empty).

    :param mediums: The list of :class:`~music_annotator.models.MBMedium` objects from the release.
    :param track_frames: Per-track CD frame start offsets extracted from ``00 - disc info.yaml``.
    :returns: The matching :class:`~music_annotator.models.MBMedium`, or ``None`` if no match is found.
    """
    for medium in mediums:
        for disc in medium.disc_list:
            if disc.offsets == track_frames:
                return medium
    return None


def _select_medium(mediums: list[MBMedium], n_src: int, src_dir_name: str, track_frames: list[int] | None = None) -> MBMedium:
    """Select the correct medium from a multi-medium release for the given source directory.

    Selection strategy (in order):

    1. **TOC match**: if ``track_frames`` is provided, compare against each medium's disc TOC entries
       via :func:`_match_medium_by_toc`.  An exact offset match unambiguously identifies the disc.
    2. **Track-count match**: if exactly one medium has ``len(track_list) == n_src``, return it.
    3. **Disc-number hint**: if multiple mediums share the same track count, check ``src_dir_name``
       for a disc-number suffix (e.g. ``"(Disc 2)"``); prefer the medium at that position.
    4. If none of the above resolves to a single medium, raise :exc:`ValueError`.

    This function should only be called when ``len(mediums) > 1``.

    :param mediums: The list of :class:`~music_annotator.models.MBMedium` objects from the release.
    :param n_src: Number of source audio files in the directory.
    :param src_dir_name: The directory basename used to extract a disc-number hint.
    :param track_frames: Optional per-track CD frame offsets from ``00 - disc info.yaml``.  When
        provided, TOC matching is attempted first before falling back to track-count heuristics.
    :returns: The selected :class:`~music_annotator.models.MBMedium`.
    :raises ValueError: When no medium in ``mediums`` has ``len(track_list) == n_src`` and TOC
        matching also fails.
    """
    # --- Priority 1: TOC match ---
    if track_frames:
        toc_match = _match_medium_by_toc(mediums, track_frames)
        if toc_match is not None:
            log.info("multi_disc_medium_selected_by_toc", position=toc_match.position, tracks=n_src)
            return toc_match

    # --- Priority 2: track-count match ---
    matching = [m for m in mediums if len(m.track_list) == n_src]
    if len(matching) == 1:
        selected = matching[0]
        log.info("multi_disc_medium_selected", position=selected.position, tracks=n_src)
        return selected
    if len(matching) > 1:
        # Multiple mediums with the same track count: try to resolve via disc-number hint in dir name.
        disc_hint_match = _DISC_SUFFIX_RE.search(src_dir_name)
        if disc_hint_match:
            hint_digits = re.search(r"\d+", disc_hint_match.group())
            hint_pos = int(hint_digits.group()) if hint_digits else 0
            hinted = [m for m in matching if m.position == hint_pos]
            selected = hinted[0] if hinted else matching[0]
        else:
            selected = matching[0]
        log.info("multi_disc_medium_selected", position=selected.position, tracks=n_src)
        return selected

    # No exact track-count match — list available mediums and abort.
    medium_info = [(m.position, len(m.track_list)) for m in mediums]
    raise ValueError(
        f"track count mismatch: source directory has {n_src} file(s) but no medium matches. "
        f"Available mediums (position, tracks): {medium_info}. "
        "Re-run with the correct --release-id for this disc, or ensure the source directory "
        "contains exactly the tracks for one medium."
    )


def _prompt_collision_policy(collisions: list[Path]) -> CollisionPolicy:
    """Print a collision warning and prompt the user for a resolution policy.

    Prints a list of the conflicting destination files and then asks the user to choose one of
    abort / skip / overwrite.  Re-prompts until a valid choice is entered.

    :param collisions: Destination files that already exist on disk.
    :returns: The :class:`CollisionPolicy` chosen by the user.
    """
    _console.print(f"\n[bold red]WARNING:[/] [red]{len(collisions)} destination file(s) already exist:[/]")
    for p in collisions:
        _console.print(f"  [red]{p}[/]")
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


def _write_sidecars(
    cover: CoverArt,
    work_top_dir: Path,
    sidecars_written: set[Path],
    journal_entries: list[TransactionEntry],
    now: str,
    release_id: str,
) -> None:
    """Write sidecar cover art files for ``work_top_dir`` and append journal entries.

    Called after every successful :func:`_verify_copy` for a track.  A :class:`~pathlib.Path`
    set ``sidecars_written`` ensures each work top directory receives its sidecar files exactly
    once per run, even when the directory contains multiple tracks.

    Writes every :class:`~music_annotator.models.CoverImage` from all non-front sidecar lists
    on ``cover`` (``front_full``, ``back``, ``booklet``, ``medium``, ``tray``, ``obi``,
    ``spine``, ``track``, ``liner``, ``sticker``, ``poster``, ``matrix``, ``top``, ``bottom``,
    ``panel``, ``watermark``, ``raw``, ``other``, ``unknown``) that has a non-empty ``filename`` field.
    For each written sidecar an ``action="downloaded"`` :class:`~music_annotator.models.TransactionEntry`
    is appended to ``journal_entries`` with ``source`` set to the canonical CAA URL (so the file
    can be re-downloaded from the journal alone).

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
    for img in sidecar_images:
        if not img.filename:
            continue
        sidecar_path = work_top_dir / img.filename
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_bytes(img.data)
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


def run(
    release_id: str,
    src_dir: Path,
    dest_root: Path,
    user_agent: str,
    dry_run: bool = False,
    fetch_rels: bool = True,
    collision_policy: CollisionPolicy = CollisionPolicy.ASK,
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
        ``00 - disc info.yaml`` (if present) against each medium's disc TOC data, then by track count, then by a
        disc-number suffix in the directory name.  A :exc:`ValueError` is raised when no medium can be matched.
    :param dest_root: Root directory of the destination music library.
    :param user_agent: User-agent string passed to :func:`init_mb`.
    :param dry_run: When ``True``, log planned operations without copying or writing any files.  MB API calls for the
        release and recording relations still happen so the planned tag data is logged accurately.  The collision prompt
        and the journal write are also skipped.
    :param fetch_rels: When ``False``, skip per-recording lookups and produce minimal tags (faster but incomplete).
        Composer, conductor, work hierarchy, and Classical Extras tags will be absent.
    :param collision_policy: How to handle pre-existing destination files.  Defaults to
        :attr:`CollisionPolicy.ASK` which prompts interactively.
    :raises mb.ResponseError: On a non-retryable MusicBrainz API error.
    :raises RuntimeError: If all retry attempts are exhausted for any API call, or if post-copy verification fails (copy
        integrity, tag round-trip, cover art, or mtime mismatch).
    :raises ValueError: If no medium in the release matches the source file count for a multi-medium release.
    :raises OSError: If source files cannot be read or destination files cannot be written.
    :raises SystemExit: With code 1 if the collision policy is ABORT (or the user chooses abort interactively).
    """
    init_mb(user_agent)

    log.info("fetch_release_start", release_id=release_id)
    release = fetch_release(release_id)
    log.info("fetch_release_done", title=release.title, date=release.date)

    src_files = find_source_files(src_dir)
    log.info("source_files", count=len(src_files))

    toc = parse_disc_toc(src_dir)
    track_frames = toc[2] if toc is not None else None

    mediums = release.medium_list
    selected_medium = mediums[0] if mediums else None

    if len(mediums) > 1:
        selected_medium = _select_medium(mediums, len(src_files), src_dir.name, track_frames=track_frames)

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
    rg_id = release.release_group.id
    cover = CoverArt()
    if not dry_run:
        cover = fetch_cover_art(release_id, rg_id)
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
            log.info("fetch_recording", position=track.position, title=track.recording.title[:60])

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

            # Compute recording_date_work: the minimum year range spanning all movements of
            # this work.  All tracks in the group use this unified value for the destination
            # directory label so movements recorded in different sessions land in the same dir.
            # The per-track RECORDING_DATE tag is NOT modified — only this path-construction
            # helper is set.
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
                    if e:  # pragma: no branch — end is always non-empty for valid ISO intervals
                        _ends.append(e)
                else:
                    _begins.append(rd)
            if _begins:
                _min_begin = min(_begins)
                _max_end = max(_ends) if _ends else ""
                _unified = f"{_min_begin}/{_max_end}" if _max_end and _max_end != _min_begin else _min_begin
                for grp_idx in group_idxs:
                    tags_map[grp_idx].recording_date_work = _unified

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
        dest_base = build_dest_path(dest_root, release, track, final_tags)
        dest_file = dest_base.with_suffix(src_file.suffix.lower())
        log.info("copy_track", src=src_file.name, dest=str(dest_file.relative_to(dest_root)))
        plan.append(CopyPlanEntry(idx=idx, src_file=src_file, dest_file=dest_file))

    # --- Collision detection and resolution ---
    skip_dest: set[Path] = set()
    if not dry_run:
        collisions = _check_collisions([e.dest_file for e in plan])
        if collisions:
            policy = collision_policy
            if policy == CollisionPolicy.ASK:
                policy = _prompt_collision_policy(collisions)
            match policy:
                case CollisionPolicy.OVERWRITE:
                    log.info("collision_overwrite", count=len(collisions))
                case CollisionPolicy.SKIP:
                    skip_dest = set(collisions)
                    log.info("collision_skip", skipped=len(skip_dest))
                case CollisionPolicy.ABORT:
                    log.warning("collision_abort")
                    raise SystemExit(1)
                case _:  # pragma: no cover
                    pass

    # --- Copy, tag, and journal ---
    journal_entries: list[TransactionEntry] = []
    sidecars_written: set[Path] = set()
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

        # Count copied (not skipped/dry-run) entries and print a confirmation message so the user
        # knows it is safe to delete the source directory before they do so.
        copied = [e for e in journal_entries if e.action == "copied"]
        dest_dirs = sorted({Path(e.destination).parent for e in copied})
        if copied:
            _console.print(f"\n[bold green]Verified OK:[/] [green]{len(copied)} file(s) written and confirmed to:[/]")
            for d in dest_dirs:
                _console.print(f"  [green]{d}[/]")
            _console.print("[bold green]It is safe to delete the source directory.[/]\n")

    log.info("run_complete", dest=str(dest_root))


# Re-export for __init__.py convenience
__all__ = [
    "CollisionPolicy",
    "run",
]
