"""Catalog-gate validation, TrackTags builder, and local-accession ingest verb for never-in-MB releases.

Provides three pieces:

(a) :func:`validate_local_tags` — reads the embedded tags of source audio files (operator
    pre-tagged with standard Vorbis/Picard vocabulary) and asserts the minimal required set is
    present and valid, raising a precise :class:`ValueError` otherwise.

(b) :func:`build_local_track_tags` — constructs a path-renderable :class:`~music_annotator.models.TrackTags`
    (and the stub :class:`~music_annotator.models.MBTrack` / :class:`~music_annotator.models.MBRelease`
    that :func:`~music_annotator._tags.build_dest_path` needs) directly from the validated embedded
    tags, without going through :func:`~music_annotator._tags.build_track_tags` (which requires a
    live MB release and is unusable here).

(c) :func:`ingest_local` — the public ingest verb for never-in-MB releases.  Calls the gate +
    builder, mints a single UUIDv4 accession ID into ``MUSICANNOTATOR_RELEASEID`` on every track,
    then flows the files through the shared copy→SHA→tag→verify→journal chain at
    ``source-tags-only`` tier (C-ACCESSION-GATE, C-LOCAL-ID, C-PROV).

C-ACCESSION-GATE (frozen): a never-in-MB release is admissible iff its embedded tags carry
the minimal required set validated by :func:`validate_local_tags`.  Required non-empty:
``ALBUMARTIST``, ``ALBUM``, per-track ``TITLE``, per-track ``TRACKNUMBER`` (unique, contiguous
1..n).  ``DATE`` is required-with-explicit-unknown: a real year, or an operator-affirmed unknown
(``date_unknown=True``), which the gate records as an affirmed empty DATE (renders no
``[rel YYYY]`` suffix).  A silently-empty DATE with no affirmation is a gate failure.
``CWP_*`` composer/work fields are optional-if-genuinely-known.

C-LOCAL-ID (frozen): the local accession UUID is minted into ``MUSICANNOTATOR_RELEASEID``
(Vorbis comment ``musicannotator_releaseid``; MP3 TXXX desc ``"MusicAnnotator Release Id"``).
It is never minted into ``MUSICBRAINZ_ALBUMID`` — the accession ID and the MB ID are independent
fields.  The UUID is retained permanently; if a real MBID later arrives it lands in
``MUSICBRAINZ_ALBUMID`` and the tier promotes under the monotonic-upgrade rule.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog

from music_annotator._pipeline import _copy_tag_verify_journal_pass
from music_annotator._pipeline_io import (
    JOURNAL_FILENAME,
    _read_tags_flac,
    _read_tags_mp3,
    append_journal_entry,
    find_source_files,
)
from music_annotator._tags import build_dest_path
from music_annotator.models import (
    CensusSignal,
    CopyPlanEntry,
    CoverArt,
    MBRelease,
    MBTrack,
    TrackTags,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_embedded_tags(path: Path) -> dict[str, str]:
    """Read embedded tags from a FLAC or MP3 file and return an uppercase-keyed dict.

    Delegates to :func:`~music_annotator._pipeline_io._read_tags_flac` for FLAC files and
    :func:`~music_annotator._pipeline_io._read_tags_mp3` for MP3 files.  Keys are uppercased;
    only non-empty values are returned.

    :param path: Path to the audio file (must have a ``.flac`` or ``.mp3`` suffix, case-insensitive).
    :returns: A ``{UPPERCASE_KEY: value}`` dict of non-empty tag values.
    :raises ValueError: If the file suffix is not ``.flac`` or ``.mp3``.
    :raises mutagen.MutagenError: If the file cannot be read.
    """
    suffix = path.suffix.lower()
    if suffix == ".flac":
        return _read_tags_flac(path)
    if suffix == ".mp3":
        return _read_tags_mp3(path)
    raise ValueError(f"Unsupported audio format: {path.suffix!r} (expected .flac or .mp3)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_local_tags(
    src_files: list[Path],
    *,
    date_unknown: bool = False,
) -> list[dict[str, str]]:
    """Read and validate the embedded tags of ``src_files`` against the C-ACCESSION-GATE required set.

    Reads the embedded tags of each source file (operator pre-tagged with standard Vorbis/Picard
    vocabulary) and asserts the minimal required set is present and valid.  Returns the per-file
    tag dicts on success; raises a precise :class:`ValueError` on the first violation found.

    Required non-empty (release-level, must be identical across all tracks):
    ``ALBUMARTIST``, ``ALBUM``.

    Required non-empty (per-track): ``TITLE``, ``TRACKNUMBER``.

    ``TRACKNUMBER`` values must be unique and contiguous from 1 to n (where n is the number of
    source files).  Gaps (e.g. ``{1, 2, 4}``) and duplicates (e.g. ``{1, 1, 2}``) are gate
    failures.

    ``DATE`` is required-with-explicit-unknown: when ``date_unknown=False`` (the default), a
    missing or empty ``DATE`` tag is a gate failure.  When ``date_unknown=True``, a missing or
    empty ``DATE`` is accepted and the returned tag dict will have no ``DATE`` key (the caller
    must set ``TrackTags.date = ""`` to render no ``[rel YYYY]`` suffix).  A real year value in
    ``DATE`` always passes regardless of ``date_unknown``.

    ``CWP_*`` composer/work fields are optional: their presence routes the composer-led path
    branch in :func:`~music_annotator._tags.build_dest_path`.

    :param src_files: Ordered list of source audio file paths (e.g. from
        :func:`~music_annotator._pipeline_io.find_source_files`).  Must be non-empty.
    :param date_unknown: When ``True``, a missing or empty ``DATE`` tag is accepted as an
        operator-affirmed unknown (renders no ``[rel YYYY]`` suffix).  When ``False`` (default),
        a missing or empty ``DATE`` is a gate failure.
    :returns: A list of ``{UPPERCASE_KEY: value}`` tag dicts, one per source file, in the same
        order as ``src_files``.  Each dict contains only non-empty values.
    :raises ValueError: If any required field is missing or empty, if ``TRACKNUMBER`` values are
        not unique and contiguous 1..n, or if ``DATE`` is missing/empty and ``date_unknown=False``.
        The error message names the missing field and the offending file path.
    :raises mutagen.MutagenError: If any source file cannot be read.
    """
    if not src_files:
        raise ValueError("src_files must be non-empty")

    all_tags: list[dict[str, str]] = []
    for path in src_files:
        all_tags.append(_read_embedded_tags(path))

    # --- Release-level required fields (must be non-empty on every track) ---
    for field in ("ALBUMARTIST", "ALBUM"):
        for path, tags in zip(src_files, all_tags):
            if not tags.get(field):
                raise ValueError(f"Required tag {field!r} is missing or empty in {path}")

    # --- Per-track required fields ---
    for field in ("TITLE", "TRACKNUMBER"):
        for path, tags in zip(src_files, all_tags):
            if not tags.get(field):
                raise ValueError(f"Required tag {field!r} is missing or empty in {path}")

    # --- TRACKNUMBER contiguity: must be unique integers 1..n ---
    track_numbers: list[int] = []
    for path, tags in zip(src_files, all_tags):
        raw_tn = tags.get("TRACKNUMBER", "")
        try:
            tn = int(raw_tn)
        except ValueError:
            raise ValueError(f"TRACKNUMBER {raw_tn!r} is not an integer in {path}") from None
        track_numbers.append(tn)

    n = len(src_files)
    sorted_tns = sorted(track_numbers)
    if sorted_tns != list(range(1, n + 1)):
        raise ValueError(f"TRACKNUMBER values {sorted(set(track_numbers))} are not unique and contiguous 1..{n}")

    # --- DATE: required-with-explicit-unknown ---
    for path, tags in zip(src_files, all_tags):
        date_val = tags.get("DATE", "")
        if not date_val and not date_unknown:
            raise ValueError(
                f"Required tag 'DATE' is missing or empty in {path}. "
                f"Supply a year or pass date_unknown=True to affirm the date is unknown."
            )

    return all_tags


def build_local_track_tags(
    src_files: list[Path],
    validated_tags: list[dict[str, str]],
    *,
    date_unknown: bool = False,
) -> list[tuple[TrackTags, MBTrack, MBRelease]]:
    """Construct path-renderable TrackTags (and API-stability stubs) from validated embedded tags.

    Builds one :class:`~music_annotator.models.TrackTags` per source file from the operator's
    embedded tags, without going through :func:`~music_annotator._tags.build_track_tags` (which
    requires a live MB release).  The returned :class:`~music_annotator.models.MBTrack` stub has
    its ``position`` set to the operator's ``TRACKNUMBER`` integer so that
    :func:`~music_annotator._tags.build_dest_path`'s leaf-number fallback chain resolves
    correctly when ``CWP_MOVT_NUM`` is absent.  The :class:`~music_annotator.models.MBRelease`
    stub is an empty default instance (the ``release`` parameter of ``build_dest_path`` is
    unread — kept for API stability only).

    ``musicbrainz_albumid`` is always ``""`` on every returned :class:`~music_annotator.models.TrackTags`
    (the never-mint-into-MB-field rule from C-LOCAL-ID).  ``musicannotator_releaseid`` is ``""``
    at this stage; the ingest verb (:func:`ingest_local`) mints the UUID and threads it in before writing.

    The ``CWP_*`` fields present in the embedded tags are threaded through to the returned
    :class:`~music_annotator.models.TrackTags` so that the composer-led path branch in
    :func:`~music_annotator._tags.build_dest_path` fires when the operator has supplied genuine
    composer/work knowledge.

    :param src_files: Ordered list of source audio file paths (same order as ``validated_tags``).
    :param validated_tags: Per-file tag dicts as returned by :func:`validate_local_tags`.
    :param date_unknown: When ``True``, an empty ``DATE`` tag is accepted and ``TrackTags.date``
        is set to ``""`` (renders no ``[rel YYYY]`` suffix).
    :returns: A list of ``(TrackTags, MBTrack, MBRelease)`` tuples, one per source file, in the
        same order as ``src_files``.
    """
    result: list[tuple[TrackTags, MBTrack, MBRelease]] = []
    stub_release = MBRelease()

    for _path, tags in zip(src_files, validated_tags):
        tn = int(tags["TRACKNUMBER"])
        date_val = tags.get("DATE", "") if not date_unknown else tags.get("DATE", "")

        # Build TrackTags from the operator's embedded tags.  Only fields that survive
        # to_file_dict() and are consumed by build_dest_path are populated here; the rest
        # default to "" per TrackTags field defaults.
        track_tags = TrackTags(
            title=tags.get("TITLE", ""),
            artist=tags.get("ARTIST", ""),
            albumartist=tags.get("ALBUMARTIST", ""),
            albumartistsort=tags.get("ALBUMARTISTSORT", ""),
            album=tags.get("ALBUM", ""),
            tracknumber=tags.get("TRACKNUMBER", ""),
            date=date_val,
            originaldate=tags.get("ORIGINALDATE", ""),
            composer=tags.get("COMPOSER", ""),
            composersort=tags.get("COMPOSERSORT", ""),
            conductor=tags.get("CONDUCTOR", ""),
            # CWP composer/work fields — optional, route composer-led path when present.
            cwp_work_top=tags.get("CWP_WORK_TOP", ""),
            cwp_workid_top=tags.get("CWP_WORKID_TOP", ""),
            cwp_part_levels=tags.get("CWP_PART_LEVELS", "0"),
            cwp_work_part_levels=tags.get("CWP_WORK_PART_LEVELS", "0"),
            cwp_part=tags.get("CWP_PART", ""),
            cwp_work=tags.get("CWP_WORK", ""),
            cwp_groupheading=tags.get("CWP_GROUPHEADING", ""),
            cwp_movt_num=tags.get("CWP_MOVT_NUM", ""),
            cwp_movt_tot=tags.get("CWP_MOVT_TOT", ""),
            cwp_composers=tags.get("CWP_COMPOSERS", ""),
            cwp_composers_sort=tags.get("CWP_COMPOSERS_SORT", ""),
            cwp_composer_lastnames=tags.get("CWP_COMPOSER_LASTNAMES", ""),
            cwp_worktype_genres=tags.get("CWP_WORKTYPE_GENRES", ""),
            cwp_worktype_genres_top=tags.get("CWP_WORKTYPE_GENRES_TOP", ""),
            # Standard fields that build_dest_path reads via to_file_dict()
            recording_date=tags.get("RECORDING_DATE", ""),
            recording_first_release_date=tags.get("RECORDING_FIRST_RELEASE_DATE", ""),
            # Accession identity: musicbrainz_albumid is always "" (C-LOCAL-ID never-mint rule).
            # musicannotator_releaseid is "" here; the ingest verb mints and threads the UUID.
            musicbrainz_albumid="",
            musicannotator_releaseid="",
        )

        # Stub MBTrack: position must equal the operator's TRACKNUMBER so that build_dest_path's
        # leaf-number fallback chain (CWP_MOVT_NUM → global_track_idx → track.position) resolves
        # correctly when CWP_MOVT_NUM is absent.
        stub_track = MBTrack(position=tn)

        result.append((track_tags, stub_track, stub_release))

    return result


def ingest_local(
    src_dir: Path,
    dest_root: Path,
    *,
    date_unknown: bool = False,
    dry_run: bool = False,
) -> None:
    """Copy, tag, and journal a never-in-MB release at the ``source-tags-only`` annotation tier.

    This is the public ingest verb for releases that have no MusicBrainz identity and will never
    acquire one.  The operator pre-tags the source directory with standard Vorbis/Picard vocabulary;
    this function validates the required tag set (C-ACCESSION-GATE), mints a single UUIDv4
    accession ID into ``MUSICANNOTATOR_RELEASEID`` on every track (C-LOCAL-ID), and flows the
    files through the shared copy→SHA→tag→verify→journal chain at ``source-tags-only`` tier
    (C-PROV).

    One accession UUID is minted per invocation and embedded in every track of the release so the
    journal stays rebuildable from tags alone.  The UUID is never minted into
    ``MUSICBRAINZ_ALBUMID`` — the accession ID and the MB ID are independent fields.

    In dry-run mode the gate still runs (gate errors surface immediately), the UUID is minted and
    logged, and the planned destination paths are logged — but no files are copied, no tags are
    written, and no journal entries are appended.

    :param src_dir: Directory containing the source audio files (FLAC or MP3), pre-tagged with
        standard Vorbis/Picard vocabulary satisfying the C-ACCESSION-GATE required set.
    :param dest_root: Root destination directory for the annotated music library.
    :param date_unknown: When ``True``, a missing or empty ``DATE`` tag is accepted as an
        operator-affirmed unknown (renders no ``[rel YYYY]`` suffix in the destination path).
        When ``False`` (default), a missing or empty ``DATE`` is a gate failure.
    :param dry_run: When ``True``, log planned operations without copying or writing any files.
        The gate still runs and the accession UUID is minted and logged.
    :raises ValueError: If the source directory fails the C-ACCESSION-GATE validation (missing
        required field, non-contiguous track numbers, or silently-missing DATE).
    :raises RuntimeError: If copy integrity, tag write, or post-copy verification fails.
    :raises OSError: If source files cannot be read or destination files cannot be written.
    """
    src_files = find_source_files(src_dir)
    log.info("local_ingest_source_files", count=len(src_files), src_dir=str(src_dir))

    # Gate: validate the required tag set.  Raises ValueError on the first violation.
    validated_tags = validate_local_tags(src_files, date_unknown=date_unknown)

    # Builder: construct TrackTags + stubs from the validated embedded tags.
    track_tuples = build_local_track_tags(src_files, validated_tags, date_unknown=date_unknown)

    # Mint one UUIDv4 accession ID for this release (C-LOCAL-ID).
    # Embedded in every track so the journal stays rebuildable from tags alone.
    # Never minted into MUSICBRAINZ_ALBUMID — the accession ID and the MB ID are independent.
    accession_id = str(uuid.uuid4())
    log.info("local_ingest_accession_id", accession_id=accession_id, dry_run=dry_run)

    # Thread the accession UUID into every TrackTags before building the copy plan.
    for track_tags, _stub_track, _stub_release in track_tuples:
        track_tags.musicannotator_releaseid = accession_id

    # Build the copy plan: one CopyPlanEntry per source file.
    # tags_map is keyed 0..n-1 over track_tuples (one release, no multi-disc aggregation).
    # global_track_idx is the 1-based position within the copy subset, threaded into
    # build_dest_path so the leaf-numbering fallback is deterministic when CWP_MOVT_NUM is absent.
    stub_release = MBRelease()
    tags_map: dict[int, TrackTags] = {}
    plan: list[CopyPlanEntry] = []

    for copy_subset_pos, (track_tags, stub_track, _stub_release) in enumerate(track_tuples):
        global_idx = copy_subset_pos
        tags_map[global_idx] = track_tags
        dest_base = build_dest_path(
            dest_root,
            stub_release,
            stub_track,
            track_tags,
            global_track_idx=copy_subset_pos + 1,
            group_modal_depth=None,
        )
        src_file = src_files[copy_subset_pos]
        dest_file = dest_base.with_suffix(src_file.suffix.lower())
        log.info(
            "local_ingest_plan_track",
            src=src_file.name,
            dest=str(dest_file.relative_to(dest_root)),
            annotation_tier="source-tags-only",
        )
        plan.append(CopyPlanEntry(idx=global_idx, src_file=src_file, dest_file=dest_file))

    if not dest_root.exists():
        if not dry_run:
            dest_root.mkdir(parents=True, exist_ok=True)
            log.info("dest_root_created", path=str(dest_root))

    # Delegate to the shared copy→SHA→tag→verify→journal implementation (C-PROV).
    # census_signal=NOT_IN_MB causes classify_annotation_tier to emit source-tags-only tier.
    # release_id carries the accession UUID so every journal "tagged" entry is rebuildable.
    journal_entries = _copy_tag_verify_journal_pass(
        plan=plan,
        tags_map=tags_map,
        cover=CoverArt(),
        src_dir=src_dir,
        dest_root=dest_root,
        release_id=accession_id,
        medium_pos=1,
        skip_dest=set(),
        dry_run=dry_run,
        acoustid_key="",
        census_signal=CensusSignal.NOT_IN_MB,
    )

    if not dry_run:
        journal_path = dest_root / JOURNAL_FILENAME
        for entry in journal_entries:
            append_journal_entry(journal_path, entry)

        tagged = [e for e in journal_entries if e.action == "tagged"]
        log.info(
            "local_ingest_complete",
            tagged=len(tagged),
            accession_id=accession_id,
            dest=str(dest_root),
        )
