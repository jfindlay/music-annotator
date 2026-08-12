"""Library maintenance operations for music-annotator.

Provides the four maintenance-mode commands that operate on an already-annotated library
without making MusicBrainz network calls:

* :func:`repath`  — re-path all verified library files to their corrected destinations.
* :func:`regroup` — consolidate confirmed split-release files into their canonical destinations.
* :func:`unify`   — consolidate performer-split and composer-split fragmented releases.
* :func:`enrich`  — retroactively backfill fingerprint fields into library files.

Also provides the shared primitives consumed by all four commands:

* :func:`_move_verify_journal`  — the single journal-append site for move-type entries (C-PROV).
* :func:`_resolve_current_lib`  — lineage walk that resolves the current on-disk path per file.
* :func:`_tags_from_file_dict`  — reconstruct a :class:`~music_annotator.models.TrackTags` from
  an on-disk tag dict.
* :func:`_hydrate_performer_lists` — reconstruct performer :class:`~music_annotator.models.ArtistEntry`
  lists from embedded tags so that :func:`~music_annotator._tags.build_dest_path` can render
  canonical entity name-forms (primary-flagged MB alias per STYLEGUIDE 3.1/NORM-2) in the
  compact path projection.

Private helpers used exclusively by :func:`unify`:

* :func:`_is_composer_split_release`
* :func:`_canonical_composer_component`
* :func:`_unify_classical_composer_groups`
"""

from __future__ import annotations

import datetime
import errno
import os
import shutil
from pathlib import Path

import structlog
from mutagen._util import MutagenError
from rich.markup import escape as _markup_escape

from music_annotator._artists import last_name
from music_annotator._audit import (
    _confirm_fragmentation,
    detect_fragmented_releases,
)
from music_annotator._console import _console
from music_annotator._mb_api import _fetch_acoustid_lookup_raw
from music_annotator._pipeline import _apply_collision_suffix
from music_annotator._pipeline_io import (
    JOURNAL_FILENAME,
    _assess_collisions,
    _needs_enrich,
    _read_duration_ms,
    _read_tags_flac,
    _read_tags_mp3,
    _sha256_file,
    _verify_copy,
    read_journal,
    write_transaction_log,
)
from music_annotator._tagger import apply_tags_flac, apply_tags_mp3
from music_annotator._tags import build_dest_path
from music_annotator.models import (
    ArtistEntry,
    CopyPlanEntry,
    MBRelease,
    MBTrack,
    TrackTags,
    TransactionEntry,
    TransactionLog,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


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


def _hydrate_performer_lists(tags: TrackTags, file_dict: dict[str, str]) -> None:
    """Reconstruct performer :class:`~music_annotator.models.ArtistEntry` lists from embedded tags.

    The ``cea_album_conductors_list``, ``cea_album_ensembles_list``, ``cea_conductors_list``, and
    ``cea_ensembles_list`` fields of :class:`~music_annotator.models.TrackTags` are internal
    in-memory fields excluded from :meth:`~music_annotator.models.TrackTags.to_file_dict` and
    therefore absent from the embedded tag dict read back from an audio file.  When
    :func:`~music_annotator._tags.build_dest_path` is called with a :class:`TrackTags` reconstructed
    from embedded tags (as in :func:`repath`, :func:`regroup`, and :func:`unify`), those lists are
    empty and the path falls back to the raw ``CEA_ENSEMBLE_NAMES`` / ``ARTIST`` string — bypassing
    the canonical name-form resolver.

    This function reconstructs the lists from the embedded string tags and MBID tags so that
    :func:`~music_annotator._tags.build_dest_path` can call
    :func:`~music_annotator._mb_api.fetch_artist_aliases` and
    :func:`~music_annotator._artists.canonical_artist_form` on each entry, rendering the
    primary-flagged MB alias (per STYLEGUIDE 3.1/NORM-2) in the compact path projection.

    MBID assignment strategy:

    * **Album-level conductors**: ``MUSICBRAINZ_CONDUCTORID`` (slash-separated) holds the MBIDs
      of all per-track conductors.  When the count of album conductor names (from
      ``CEA_ALBUM_CONDUCTORS``) equals the count of conductor MBIDs, the two sequences are zipped
      positionally.  Otherwise entries are created without MBIDs and the resolver falls back to the
      as-credited name.
    * **Album-level ensembles**: ``MUSICBRAINZ_ALBUMARTISTID`` (slash-separated) holds the MBIDs
      of all album artists (from ``release.artist_credit``).  Conductor MBIDs are subtracted
      (order-preserving) to isolate ensemble MBIDs.  When the count of album ensemble names (from
      ``CEA_ALBUM_ENSEMBLES``) equals the count of remaining MBIDs, the two sequences are zipped
      positionally.  Otherwise entries are created without MBIDs.
    * **Per-track conductors/ensembles** (``cea_conductors_list`` / ``cea_ensembles_list``): the
      same strategy is applied using ``CEA_CONDUCTORS`` / ``CEA_ENSEMBLES`` names and the full
      ``MUSICBRAINZ_CONDUCTORID`` / ``MUSICBRAINZ_ALBUMARTISTID`` MBID sets.

    Mutates ``tags`` in-place; returns ``None``.  The function is idempotent: calling it on a
    ``TrackTags`` that already has non-empty lists is a no-op (the lists are only set when they
    are currently empty, which is always the case for tags reconstructed from embedded files).

    :param tags: The :class:`~music_annotator.models.TrackTags` instance to hydrate, as returned
        by :func:`_tags_from_file_dict`.
    :param file_dict: The uppercase ``{KEY: value}`` mapping read back from the audio file,
        as returned by :func:`~music_annotator._pipeline_io._read_tags_flac` or
        :func:`~music_annotator._pipeline_io._read_tags_mp3`.
    """

    def _split_semi(raw: str) -> list[str]:
        """Split a semicolon-separated tag value into a list of non-empty stripped parts.

        :param raw: A semicolon-separated string (e.g. ``"Karajan; Abbado"``).
        :returns: List of non-empty stripped parts.
        """
        return [p.strip() for p in raw.split(";") if p.strip()]

    def _split_slash(raw: str) -> list[str]:
        """Split a slash-separated MBID tag value into a list of non-empty stripped parts.

        :param raw: A slash-separated string (e.g. ``"mbid-1/mbid-2"``).
        :returns: List of non-empty stripped parts.
        """
        return [p.strip() for p in raw.split("/") if p.strip()]

    def _make_entries(names: list[str], sorts: list[str], mbids: list[str]) -> list[ArtistEntry]:
        """Build :class:`~music_annotator.models.ArtistEntry` objects from parallel name/sort/MBID lists.

        When ``mbids`` has the same length as ``names``, each entry receives its MBID so that
        :func:`~music_annotator._tags.build_dest_path` can hydrate it via
        :func:`~music_annotator._mb_api.fetch_artist_aliases`.  When lengths differ, entries are
        created without MBIDs and the canonical-form resolver falls back to the as-credited name.

        :param names: Display names (from ``CEA_ALBUM_CONDUCTORS`` etc.).
        :param sorts: Sort names (from ``CEA_ALBUM_CONDUCTORS_SORT`` etc.); padded with ``""`` when
            shorter than ``names``.
        :param mbids: MBID strings; may be empty or a different length from ``names``.
        :returns: A list of :class:`~music_annotator.models.ArtistEntry` objects.
        """
        use_mbids = len(mbids) == len(names)
        entries: list[ArtistEntry] = []
        for i, name in enumerate(names):
            sort = sorts[i] if i < len(sorts) else ""
            mbid = mbids[i] if use_mbids else ""
            entries.append(ArtistEntry(name=name, sort=sort or name, mbid=mbid))
        return entries

    # --- Album-level conductors ---
    album_conductor_names = _split_semi(file_dict.get("CEA_ALBUM_CONDUCTORS", ""))
    album_conductor_sorts = _split_semi(file_dict.get("CEA_ALBUM_CONDUCTORS_SORT", ""))
    conductor_mbids = _split_slash(file_dict.get("MUSICBRAINZ_CONDUCTORID", ""))

    # --- Album-level ensembles ---
    # Ensemble MBIDs: album artist MBIDs minus conductor MBIDs (order-preserving subtraction).
    album_artist_mbids = _split_slash(file_dict.get("MUSICBRAINZ_ALBUMARTISTID", ""))
    conductor_mbid_set = set(conductor_mbids)
    ensemble_mbids = [m for m in album_artist_mbids if m not in conductor_mbid_set]

    album_ensemble_names = _split_semi(file_dict.get("CEA_ALBUM_ENSEMBLES", ""))
    album_ensemble_sorts = _split_semi(file_dict.get("CEA_ALBUM_ENSEMBLES_SORT", ""))

    # --- Per-track conductors and ensembles ---
    conductor_names = _split_semi(file_dict.get("CEA_CONDUCTORS", ""))
    conductor_sorts: list[str] = []  # no sort tag for per-track conductors in embedded tags
    ensemble_names = _split_semi(file_dict.get("CEA_ENSEMBLES", ""))
    ensemble_sorts = _split_semi(file_dict.get("CEA_ENSEMBLES_SORT", ""))

    # Reconstruct lists (only when currently empty — tags from embedded files always start empty).
    if not tags.cea_album_conductors_list and album_conductor_names:
        tags.cea_album_conductors_list = _make_entries(album_conductor_names, album_conductor_sorts, conductor_mbids)
    if not tags.cea_album_ensembles_list and album_ensemble_names:
        tags.cea_album_ensembles_list = _make_entries(album_ensemble_names, album_ensemble_sorts, ensemble_mbids)
    if not tags.cea_conductors_list and conductor_names:
        tags.cea_conductors_list = _make_entries(conductor_names, conductor_sorts, conductor_mbids)
    if not tags.cea_ensembles_list and ensemble_names:
        tags.cea_ensembles_list = _make_entries(ensemble_names, ensemble_sorts, ensemble_mbids)


def _resolve_current_lib(journal: TransactionLog) -> dict[Path, str]:
    """Resolve the current on-disk path for each logical library file from the journal.

    Walks journal entries in chronological order to build a mapping from each file's current
    on-disk path to its associated release MBID.  The walk handles the full lineage chain:

    * ``"tagged"`` entries seed the map (destination → release_id).
    * ``"repathed"`` and ``"regrouped"`` entries update the map: the old path is removed and the
      new path is registered with the same release_id.
    * ``"enriched"`` entries are in-place updates (source == destination); they re-register the
      path to keep the release_id current.

    Multi-hop chains (a file that was repathed and then regrouped) resolve correctly because
    entries are processed in chronological order: each move pops the old path and registers the
    new one, so the map always reflects the latest known location.

    :param journal: The :class:`~music_annotator.models.TransactionLog` to walk.
    :returns: A ``{current_path: release_id}`` mapping for all logical library files.
    """
    current_lib: dict[Path, str] = {}

    for entry in journal.entries:
        dest_path = Path(entry.destination)
        if entry.action == "tagged":
            current_lib[dest_path] = entry.release_id
        elif entry.action in {"repathed", "regrouped"}:
            old_path = Path(entry.source)
            release_id_for_path = current_lib.pop(old_path, entry.release_id)
            current_lib[dest_path] = release_id_for_path
        elif entry.action == "enriched":
            # In-place update: source == destination, path unchanged.
            # Re-register to keep release_id current.
            current_lib[dest_path] = entry.release_id

    return current_lib


def _move_verify_journal(
    plan_pairs: list[tuple[Path, Path]],
    *,
    journal_path: Path,
    action: str,
    dest_root: Path,
    now: datetime.datetime,
    release_id: str = "",
) -> int:
    """Move each ``(src, dest)`` pair atomically, verify integrity, and journal each success.

    This is the single site that may append move-type journal entries (``"repathed"``,
    ``"regrouped"``, ``"unified"``), enforcing the C-PROV provenance-chain invariant: a journal
    entry is written **only after** the file passes both the SHA-256 destination check and
    :func:`~music_annotator._pipeline_io._verify_copy`.

    For each pair the sequence is:

    1. Capture source SHA-256 and mtime before the move.
    2. Ensure the destination parent directory exists.
    3. Move atomically via :func:`os.replace` (rename within the same filesystem).  On
       ``OSError`` with ``errno.EXDEV`` (cross-filesystem move), fall back to
       :func:`shutil.copy2` + :func:`os.unlink`; the copy is integrity-checked before the
       source is unlinked.
    4. Verify destination SHA-256 == source SHA-256 (raises :exc:`RuntimeError` on mismatch —
       **no journal entry is written**).
    5. Read back the destination tags and run :func:`~music_annotator._pipeline_io._verify_copy`
       (raises :exc:`RuntimeError` on mismatch — **no journal entry is written**).
    6. **Only then** append a :class:`~music_annotator.models.TransactionEntry` with the given
       ``action`` and ``release_id`` and flush it to the journal.
    7. Clean up now-empty source directories (best-effort; non-empty directories are skipped).

    :param plan_pairs: List of ``(src, dest)`` path pairs to move.
    :param journal_path: Path to the journal file (``<dest_root>/music_annotator_journal.json``).
    :param action: Journal action string (e.g. ``"repathed"``, ``"regrouped"``, ``"unified"``).
    :param dest_root: Root of the destination library; used for empty-directory cleanup and
        log messages.
    :param now: UTC datetime for the journal entry timestamp (ISO-format string is derived from
        this value).
    :param release_id: MusicBrainz release MBID for the journal entry.  Empty string for
        ``"repathed"`` entries (repath operates offline from embedded tags).
    :returns: Count of files successfully moved and journalled.
    :raises RuntimeError: If the post-move SHA-256 check or :func:`_verify_copy` fails.
    :raises OSError: If the source file cannot be read or the destination cannot be written
        (except ``EXDEV``, which is handled by the cross-filesystem fallback).
    """
    now_str = now.isoformat()
    moved_count = 0

    for src, dest in plan_pairs:
        # a. Capture source SHA-256 and mtime before the move.
        src_hash = _sha256_file(src)
        src_stat = src.stat()
        src_mtime = src_stat.st_mtime

        # b. Ensure parent directory exists; move atomically.
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # Cross-filesystem fallback: copy + verify + unlink.
            shutil.copy2(src, dest)
            cross_hash = _sha256_file(dest)
            if cross_hash != src_hash:
                dest.unlink(missing_ok=True)
                raise RuntimeError(
                    f"cross-fs copy integrity failure for '{src.name}': "
                    f"src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {cross_hash[:12]}…"
                ) from exc
            os.unlink(src)

        # c. Verify destination SHA-256 == source SHA-256.
        dest_hash = _sha256_file(dest)
        if dest_hash != src_hash:
            raise RuntimeError(
                f"{action} integrity failure for '{dest.name}': src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {dest_hash[:12]}…"
            )

        # d. Reconstruct tags for _verify_copy (tags are unchanged by the move).
        ext = src.suffix.lower()
        try:
            match ext:
                case ".flac":
                    post_dict = _read_tags_flac(dest)
                case ".mp3":
                    post_dict = _read_tags_mp3(dest)
                case _:  # pragma: no cover
                    post_dict = {}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{action} tag re-read failure for '{dest.name}': {exc}") from exc
        moved_tags = _tags_from_file_dict(post_dict)
        # _verify_copy checks mtime; for os.replace (same-fs rename) mtime is preserved.
        # For cross-fs copy2+unlink, shutil.copy2 copies atime/mtime so src_mtime still holds.
        _verify_copy(src, dest, moved_tags, None, src_mtime)

        # e. Journal the move and flush before proceeding to the next file (C-PROV invariant:
        #    entry is written ONLY after _verify_copy passes).
        entry = TransactionEntry(
            timestamp=now_str,
            release_id=release_id,
            source=str(src),
            destination=str(dest),
            action=action,
        )
        write_transaction_log(journal_path, [entry])
        log.info(
            f"{action}_moved",
            old=str(src.relative_to(dest_root)) if src.is_relative_to(dest_root) else str(src),
            new=str(dest.relative_to(dest_root)),
        )
        moved_count += 1

        # f. Clean up now-empty source directories (best-effort; non-empty dirs are skipped).
        src_dir = src.parent
        while src_dir != dest_root and src_dir.is_relative_to(dest_root):
            try:
                src_dir.rmdir()  # Only succeeds if directory is now empty.
                log.info(f"{action}_removed_empty_dir", dir=str(src_dir.relative_to(dest_root)))
                src_dir = src_dir.parent
            except OSError:
                break

    return moved_count


def repath(dest_root: Path, *, dry_run: bool = False, yes: bool = False) -> None:
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

    When ``yes`` is ``False`` (the default), a confirmation prompt listing all planned moves is
    shown before any files are moved.  Pass ``yes=True`` (or ``-y``/``--yes`` on the CLI) to
    skip the prompt and proceed immediately.

    .. warning::
        A bare ``repath <dest>`` invocation **mass-relocates the entire library**.  The
        ``action="repathed"`` journal entries are the complete recovery record — if something goes
        wrong, examine ``music_annotator_journal.json`` in ``dest_root`` to reconstruct what
        moved where.  Use ``--dry-run`` first to preview all planned moves.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param dry_run: When ``True``, log planned moves without performing any filesystem
        operations or writing journal entries.
    :param yes: When ``True``, skip the confirmation prompt and move files immediately.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = read_journal(journal_path)

    # --- Determine the current canonical path for each logical file ---
    # _resolve_current_lib walks entries in chronological order; "tagged" seeds the map and
    # "repathed"/"regrouped" entries update it.  Multi-hop chains resolve naturally because
    # each move pops the old path and registers the new one.
    current_lib = _resolve_current_lib(journal)

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

        # Reconstruct performer ArtistEntry lists from embedded tags so that build_dest_path
        # can render canonical entity name-forms (primary-flagged MB alias per STYLEGUIDE
        # 3.1/NORM-2) in the compact path projection.  The list fields are excluded from
        # to_file_dict() and therefore absent from the embedded tag dict; without hydration,
        # build_dest_path falls back to the raw CEA_ENSEMBLE_NAMES / ARTIST string.
        _hydrate_performer_lists(tags, file_dict)

        # Construct minimal stand-in objects for build_dest_path.
        # release is kept for API stability (C-INIT removed the last internal use of
        # release.artist_credit in the classical path).  track.position is used only as the
        # deepest leaf-nn fallback (when CWP_MOVT_NUM is absent and global_track_idx=0); zero
        # is acceptable here because CWP_MOVT_NUM must be present for the repath to produce a
        # meaningful path.
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

    # --- Intra-plan collision guard (legitimate partial-performance collisions) ---
    # When two or more files in the plan recompute to the same clean destination, they are
    # likely legitimate partial-performance collisions: different recordings of the same work
    # that were previously disambiguated by a collision suffix on the work_dir.  Moving them
    # would cause one to overwrite the other (os.replace is atomic and silently clobbers).
    # Guard: skip all files in such groups — they are already at valid (collision-suffixed)
    # locations and must not lose their disambiguation.
    #
    # This is distinct from the existing on-disk collision detection below, which handles
    # the case where a planned destination already exists on disk before any moves begin.
    # Intra-plan collisions are invisible to _assess_collisions because neither destination
    # exists on disk yet when the plan is built.
    _dest_to_plan_indices: dict[Path, list[int]] = {}
    for _i, (_, _dest, _, _) in enumerate(plan_pairs):
        _dest_to_plan_indices.setdefault(_dest, []).append(_i)

    _intra_collision_indices: set[int] = set()
    for _dest, _indices in _dest_to_plan_indices.items():
        if len(_indices) > 1:
            for _idx in _indices:
                _intra_collision_indices.add(_idx)
            log.info(
                "repath_intra_plan_collision_skipped",
                dest=str(_dest.relative_to(dest_root)),
                count=len(_indices),
            )

    if _intra_collision_indices:
        plan_pairs = [pair for _i, pair in enumerate(plan_pairs) if _i not in _intra_collision_indices]
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

    # --- Confirmation prompt ---
    if not yes:
        _console.print("\n[bold yellow]repath[/] will move the following files:\n")
        for current_path, new_dest, _, _ in plan_pairs:
            _console.print(
                f"  [dim]{_markup_escape(str(current_path.relative_to(dest_root)))}[/]\n"
                f"    → [green]{_markup_escape(str(new_dest.relative_to(dest_root)))}[/]"
            )
        _console.print(f"\n[bold]{len(plan_pairs)} file(s) will be moved.[/]  Proceed? [dim](y/n)[/]")
        _console.print("\n[bold cyan]>[/] ", end="")
        answer = input("").strip().lower()
        if answer not in {"y", "yes"}:
            log.info("repath_aborted", dest_root=str(dest_root))
            return

    # --- Perform moves, verify, journal ---
    now = datetime.datetime.now(datetime.UTC)
    move_pairs = [(src, dest) for src, dest, _, _ in plan_pairs]
    moved = _move_verify_journal(
        move_pairs,
        journal_path=journal_path,
        action="repathed",
        dest_root=dest_root,
        now=now,
        release_id="",
    )
    log.info("repath_complete", dest_root=str(dest_root), moved=moved)


def regroup(dest_root: Path, *, yes: bool = False, dry_run: bool = False) -> None:
    """Consolidate confirmed split-release files into their canonical destinations.

    Reads the transaction journal, runs the tag-confirmation fragmentation audit
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
    # _resolve_current_lib resolves the full library lineage; filter to confirmed release IDs.
    full_lib = _resolve_current_lib(journal)
    current_lib: dict[Path, str] = {p: rid for p, rid in full_lib.items() if rid in confirmed_release_ids}

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

        # Reconstruct performer ArtistEntry lists from embedded tags so that build_dest_path
        # renders canonical entity name-forms (primary-flagged MB alias per STYLEGUIDE 3.1/NORM-2).
        _hydrate_performer_lists(tags, file_dict)

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
    # regroup is release-driven: each file may belong to a different release_id, so we call
    # _move_verify_journal once per unique release_id group to preserve the per-entry release_id.
    now = datetime.datetime.now(datetime.UTC)
    total_moved = 0
    # Group plan_pairs by release_id so each batch shares the same journal release_id.
    release_groups: dict[str, list[tuple[Path, Path]]] = {}
    for src, dest, _, _, rid in plan_pairs:
        release_groups.setdefault(rid, []).append((src, dest))

    for rid, move_pairs in release_groups.items():
        total_moved += _move_verify_journal(
            move_pairs,
            journal_path=journal_path,
            action="regrouped",
            dest_root=dest_root,
            now=now,
            release_id=rid,
        )
    log.info("regroup_complete", dest_root=str(dest_root), moved=total_moved)


def _is_composer_split_release(group_tags: list[tuple[Path, TrackTags, dict[str, str]]]) -> bool:
    """Return ``True`` when a release group is a multi-composer compilation (non-classical).

    A release is a multi-composer compilation when ``CEA_COMPOSER_LASTNAMES`` is non-empty and
    takes ≥2 distinct values across the tracks of the group, AND the release is confirmed
    non-classical.

    The non-classical scope gate: the release is non-classical when **any** track in the group
    satisfies either of:

    * ``cwp_work_top`` is empty (no MB work link → non-classical), or
    * ``cwp_worktype_genres_top`` does not contain ``"Classical"``.

    A classical release with varying ``CEA_COMPOSER_LASTNAMES`` (e.g. a multi-composer anthology)
    routes through the existing cross-medium composer-pass unification (W2c), not this rule.

    :param group_tags: List of ``(file_path, tags, file_dict)`` triples for all files in the
        release group, as built by :func:`unify`.
    :returns: ``True`` when the group is a non-classical multi-composer compilation.
    """
    # Collect distinct non-empty CEA_COMPOSER_LASTNAMES values
    composer_values: set[str] = set()
    for _, tags, _ in group_tags:
        if tags.cea_composer_lastnames:
            composer_values.add(tags.cea_composer_lastnames)

    if len(composer_values) < 2:  # noqa: PLR2004 — 2 is the multi-composer threshold (C-W2)
        return False

    # Scope gate: apply only to non-classical releases.
    # A release is non-classical when any track lacks a CWP_WORK_TOP (no MB work link) OR
    # its CWP_WORKTYPE_GENRES_TOP does not contain "Classical".
    for _, tags, _ in group_tags:
        if not tags.cwp_work_top:
            return True
        if "Classical" not in tags.cwp_worktype_genres_top:
            return True

    return False


def _canonical_composer_component(group_tags: list[tuple[Path, TrackTags, dict[str, str]]]) -> str:
    """Derive the canonical composer path component for a multi-composer compilation.

    Reads ``ALBUMARTISTSORT`` from the first track in the group (it is uniform across a release)
    and applies :func:`~music_annotator._artists.last_name` to produce the sort-name last-name
    form.

    Fallback: if ``ALBUMARTISTSORT`` is empty or ``"Various Artists"``, returns ``"Various"``
    (the CE convention for multi-artist compilations with no single canonical identity).

    :param group_tags: List of ``(file_path, tags, file_dict)`` triples for all files in the
        release group.  Must be non-empty.
    :returns: The canonical composer component string (e.g. ``"Goodman, Benny"`` or
        ``"Various"``).
    """
    # Read ALBUMARTISTSORT from the first track (uniform across a release)
    _, first_tags, _ = group_tags[0]
    album_artist_sort = first_tags.albumartistsort.strip()

    if not album_artist_sort or album_artist_sort == "Various Artists":
        return "Various"

    return last_name(album_artist_sort)


def _unify_classical_composer_groups(group_tags: list[tuple[Path, TrackTags, dict[str, str]]]) -> None:
    """Propagate the plurality ``cea_composer_lastnames`` within each top-work group for classical releases.

    Implements the W2c arranger/finisher retroactive fix for already-annotated libraries.  When a
    classical release has movements where an arranger or finisher was credited as ``"composer"``
    with the ``"additional"`` attribute on only some movements, those movements may have a different
    ``CEA_COMPOSER_LASTNAMES`` embedded in their tags than the movements with a plain primary-composer
    relation (the Mozart K.626 Süßmayr shape).

    Because ``cwp_composers_is_fallback`` is never written to audio files (it is an in-memory
    pipeline flag only), the retroactive pass cannot distinguish primary from fallback credits
    directly.  Instead, it uses the **plurality value** within each top-work group: the most
    frequently occurring non-empty ``cea_composer_lastnames`` value across all movements of the
    same ``cwp_workid_top`` is taken as the canonical composer, and all movements that differ are
    patched to match.

    This mirrors the cross-medium composer pass in :func:`run` (which propagates the primary
    composer from movements that have one to movements that used the fallback), but operates on
    already-embedded tags rather than in-memory :class:`~music_annotator.models.TrackTags` objects
    built during annotation.

    Mutates ``group_tags`` in-place (patches both ``tags.cea_composer_lastnames`` and
    ``tags.cwp_composer_lastnames`` on affected entries, since :func:`~music_annotator._tags.build_dest_path`
    prefers ``CWP_COMPOSER_LASTNAMES`` over ``CEA_COMPOSER_LASTNAMES`` when both are present).
    Only called for classical releases (scope gate is in :func:`unify`).

    :param group_tags: List of ``(file_path, tags, file_dict)`` triples for all files in the
        release group, as built by :func:`unify`.
    """
    # Group tracks by top-work MBID (cwp_workid_top, falling back to musicbrainz_workid).
    # Tracks without any work ID are grouped under "" and skipped (no work context to unify).
    work_groups: dict[str, list[int]] = {}
    for i, (_, tags, _) in enumerate(group_tags):
        work_id = tags.cwp_workid_top or tags.musicbrainz_workid
        work_groups.setdefault(work_id, []).append(i)

    for work_id, idxs in work_groups.items():
        if not work_id:
            continue  # no work context — skip

        # Count occurrences of each non-empty cea_composer_lastnames value in this work group.
        composer_counts: dict[str, int] = {}
        for i in idxs:
            _, tags, _ = group_tags[i]
            val = tags.cea_composer_lastnames
            if val:
                composer_counts[val] = composer_counts.get(val, 0) + 1

        if len(composer_counts) < 2:  # noqa: PLR2004 — 2 is the multi-value threshold
            continue  # all movements agree — nothing to unify

        # Plurality value: most common non-empty cea_composer_lastnames in this work group.
        # Ties are broken by first-appearance order (stable: dict preserves insertion order in
        # Python 3.7+, and we iterate group_tags in file-path order).
        # Use __getitem__ directly to avoid the cell-var-from-loop pylint warning that would
        # arise from a lambda capturing composer_counts by reference inside the loop.
        canonical = max(composer_counts, key=composer_counts.__getitem__)

        log.info(
            "unify_classical_composer_group",
            work_id=work_id,
            canonical=canonical,
            counts=composer_counts,
        )

        for i in idxs:
            _, tags, _ = group_tags[i]
            # Patch both CEA_COMPOSER_LASTNAMES and CWP_COMPOSER_LASTNAMES so that
            # build_dest_path (which prefers CWP_COMPOSER_LASTNAMES) produces the
            # canonical path for all movements.
            if tags.cea_composer_lastnames != canonical:
                tags.cea_composer_lastnames = canonical
            if tags.cwp_composer_lastnames != canonical:
                tags.cwp_composer_lastnames = canonical


def unify(dest_root: Path, *, yes: bool = False, dry_run: bool = False) -> None:
    """Consolidate performer-split and composer-split fragmented releases into their canonical top_dirs.

    Scans ``dest_root`` for releases whose tracks are spread across ≥2 distinct top_dirs due to
    per-track ``CEA_SOLOISTS`` variation (the dominant fragmentation shape: 29 releases in the 2026-06 audit)
    or per-track ``CEA_COMPOSER_LASTNAMES`` variation on non-classical compilations (the Benny
    Goodman shape).  For each fragmented release, reads the embedded tags from all its files, runs
    :func:`~music_annotator._tags.build_dest_path` over the full release group to compute the
    canonical destination for every file, and moves files that are not already at their canonical
    path.

    **Detection (C-W2):** a release is fragmented when ≥2 distinct top_dirs share the same
    ``MUSICBRAINZ_ALBUMID`` tag.  The join key is the embedded tag, not the journal.

    **Canonical path algorithm (C-W2):** :func:`~music_annotator._tags.build_dest_path` already
    computes the correct unified path when given all tracks of the release as a group, because the
    cross-medium composer pass and ``recording_date_work`` pass run over the full release group.
    The performers component uses album-level conductors and ensembles (C-NOSOLO: soloists are
    never a path component).

    **Composer-split pre-processing (W2b):** when ``CEA_COMPOSER_LASTNAMES`` varies across tracks
    of a non-classical release, :func:`_is_composer_split_release` detects the shape and
    :func:`_canonical_composer_component` derives the canonical composer component from
    ``ALBUMARTISTSORT`` (falling back to ``"Various"``).  Every track's ``cea_composer_lastnames``
    is patched to this canonical value before :func:`~music_annotator._tags.build_dest_path` is
    called.  ``build_dest_path`` itself is unchanged.

    **Classical arranger/finisher unification (W2c):** for classical releases where
    ``CEA_COMPOSER_LASTNAMES`` varies across movements of the same top work (the Mozart K.626
    Süßmayr shape — an arranger/finisher credited as ``"composer"`` with the ``"additional"``
    attribute on only some movements), :func:`_unify_classical_composer_groups` propagates the
    plurality composer value within each ``CWP_WORKID_TOP`` group.  This is the retroactive
    counterpart to the cross-medium composer pass in :func:`run`: files annotated before that pass
    was implemented may have an arranger/finisher's name embedded as the composer for some
    movements.  The W2c pass runs only when W2b does not apply (i.e. the release is classical or
    has uniform ``CEA_COMPOSER_LASTNAMES``).

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

        # --- Composer-split pre-processing (W2b) ---
        # When CEA_COMPOSER_LASTNAMES varies across tracks of a non-classical release, patch
        # every track's tags with the canonical composer component derived from ALBUMARTISTSORT
        # before calling build_dest_path.  This ensures all tracks land in the same top_dir.
        # build_dest_path itself is unchanged — the normalisation is pre-processing only.
        if _is_composer_split_release(group_tags):
            canonical_composer = _canonical_composer_component(group_tags)
            log.info(
                "unify_composer_split_detected",
                release_id=release_id,
                canonical_composer=canonical_composer,
            )
            for _, tags, _ in group_tags:
                tags.cea_composer_lastnames = canonical_composer

        # --- Classical arranger/finisher unification (W2c) ---
        # For classical releases where CEA_COMPOSER_LASTNAMES varies across movements of the
        # same top work, propagate the plurality composer value within each work group.  This
        # is the retroactive counterpart to the cross-medium composer pass in run(): files
        # annotated before that pass was implemented may have an arranger/finisher's name
        # embedded as the composer for some movements (the Mozart K.626 Süßmayr shape).
        # _is_composer_split_release() returns False for classical releases, so this pass
        # runs independently of W2b and handles the classical case only.
        # The scope gate (classical release) is enforced inside _unify_classical_composer_groups
        # by checking that at least one track has cwp_work_top set and cwp_worktype_genres_top
        # contains "Classical".  We pre-check here to avoid the function call overhead when
        # the release is clearly non-classical (already handled by W2b above).
        else:
            _unify_classical_composer_groups(group_tags)

        # Compute canonical destinations for every file in the group.
        # build_dest_path uses the path fields (recording_date_work, etc.) already embedded
        # in the tags from the original annotation pipeline run.
        # global_track_idx=0 is acceptable here because CWP_MOVT_NUM is present in the tags
        # for properly annotated files (same as repath/regroup).
        stub_release = MBRelease()
        stub_track = MBTrack()

        for file_path, tags, file_dict in group_tags:
            ext = file_path.suffix.lower()
            # Reconstruct performer ArtistEntry lists from embedded tags so that build_dest_path
            # renders canonical entity name-forms (primary-flagged MB alias per STYLEGUIDE
            # 3.1/NORM-2) in the compact path projection.
            _hydrate_performer_lists(tags, file_dict)
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
    # unify is release-driven: each file may belong to a different release_id, so we call
    # _move_verify_journal once per unique release_id group to preserve the per-entry release_id.
    now = datetime.datetime.now(datetime.UTC)
    total_moved = 0
    release_groups: dict[str, list[tuple[Path, Path]]] = {}
    for src, dest, _, _, rid in plan_pairs:
        release_groups.setdefault(rid, []).append((src, dest))

    for rid, move_pairs in release_groups.items():
        total_moved += _move_verify_journal(
            move_pairs,
            journal_path=journal_path,
            action="unified",
            dest_root=dest_root,
            now=now,
            release_id=rid,
        )
    log.info("unify_complete", dest_root=str(dest_root), moved=total_moved)


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
    # _resolve_current_lib walks entries in chronological order; "tagged" seeds the map;
    # "repathed"/"regrouped" update it; "enriched" re-registers the path with the current
    # release_id.  Multi-hop chains resolve naturally.
    current_lib = _resolve_current_lib(journal)

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
        # Cannot-determine failures (5xx exhaustion, malformed JSON) are logged and skipped so
        # that a transient AcoustID outage does not abort the entire enrich run.
        if re_resolve and acoustid_key and "chromaprint_fp" in write_fields:
            _enrich_fp = write_fields["chromaprint_fp"]
            _enrich_dur_s = _read_duration_ms(current_path) // 1000
            try:
                _, _enrich_top_uuid = _fetch_acoustid_lookup_raw(_enrich_fp, _enrich_dur_s, acoustid_key)
            except (OSError, RuntimeError, ValueError) as _exc:
                log.warning(
                    "enrich_acoustid_lookup_failed",
                    path=str(current_path.relative_to(dest_root)),
                    error=str(_exc),
                )
                _enrich_top_uuid = ""
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
