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


def _select_medium(mediums: list[MBMedium], n_src: int, src_dir_name: str) -> MBMedium:
    """Select the correct medium from a multi-medium release for the given source directory.

    Selection strategy (in order):

    1. If exactly one medium matches the source file count, return it.
    2. If multiple mediums match, prefer the one whose position matches any disc-number hint in
       ``src_dir_name`` (e.g. ``"(Disc 1)"``); otherwise return the first match.
    3. If no medium matches, raise :exc:`ValueError` listing the available mediums.

    This function should only be called when ``len(mediums) > 1``.

    :param mediums: The list of :class:`~music_annotator.models.MBMedium` objects from the release.
    :param n_src: Number of source audio files in the directory.
    :param src_dir_name: The directory basename used to extract a disc-number hint.
    :returns: The selected :class:`~music_annotator.models.MBMedium`.
    :raises ValueError: When no medium in ``mediums`` has ``len(track_list) == n_src``.
    """
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

    Writes every :class:`~music_annotator.models.CoverImage` from ``cover.front_full``,
    ``cover.back``, ``cover.booklet``, and ``cover.medium`` that has a non-empty ``filename``
    field.  For each written sidecar a ``action="downloaded"`` :class:`~music_annotator.models.TransactionEntry`
    is appended to ``journal_entries`` with ``source`` set to the canonical CAA URL stored on the image
    (so the file can be re-downloaded from the journal alone).

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

    sidecar_images: list[CoverImage] = list(cover.front_full) + list(cover.back) + list(cover.booklet) + list(cover.medium)
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
        order.  For multi-medium releases the medium whose track count matches ``len(src_files)`` is selected
        automatically; when no medium matches a :exc:`ValueError` is raised listing the available options.
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

    mediums = release.medium_list
    selected_medium = mediums[0] if mediums else None

    if len(mediums) > 1:
        selected_medium = _select_medium(mediums, len(src_files), src_dir.name)

    if selected_medium is None:
        raise ValueError(f"release '{release.title}' has no mediums")

    medium_pos = selected_medium.position
    all_track_pairs: list[tuple[MBTrack, int]] = [(t, medium_pos) for t in selected_medium.track_list]
    log.info("release_tracks", count=len(all_track_pairs), disc=medium_pos)

    if len(src_files) != len(all_track_pairs):
        log.warning(
            "track_count_mismatch",
            src_files=len(src_files),
            release_tracks=len(all_track_pairs),
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
        final_tags.coverart_front_file = cover.front_full[0].filename if cover.front_full else ""
        final_tags.coverart_back_file = "; ".join(img.filename for img in cover.back if img.filename)
        final_tags.coverart_booklet_files = "; ".join(img.filename for img in cover.booklet if img.filename)
        final_tags.coverart_medium_files = "; ".join(img.filename for img in cover.medium if img.filename)

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
